//! Shared deterministic native ROI selection and diagnostics. No quality acceptance thresholds.
use super::{
    image_ops::{gaussian, mean, round6, std},
    renderer::MAX_EDGE,
    RgbImage,
};
use serde_json::{json, Value};
pub(super) const ROI_FRAC: f64 = 0.125;
const RESIDUAL_SIGMA: f64 = 1.1;

/// 正規化座標 [x0, y0, x1, y1] を w×h の画素矩形 (x, y, rw, rh) にする。
pub(super) fn rect(frac: [f64; 4], w: usize, h: usize) -> (usize, usize, usize, usize) {
    let min_w = w.min(8);
    let min_h = h.min(8);
    let x0 = ((frac[0] * w as f64).round() as usize).min(w.saturating_sub(min_w));
    let y0 = ((frac[1] * h as f64).round() as usize).min(h.saturating_sub(min_h));
    let x1 = ((frac[2] * w as f64).round() as usize).clamp(x0 + min_w, w);
    let y1 = ((frac[3] * h as f64).round() as usize).clamp(y0 + min_h, h);
    (x0, y0, x1 - x0, y1 - y0)
}
pub(super) fn crop_u8(src: &image::RgbImage, frac: [f64; 4]) -> Result<RgbImage, String> {
    let (x, y, w, h) = rect(frac, src.width() as usize, src.height() as usize);
    let mut pixels = Vec::with_capacity(w * h);
    for yy in y..y + h {
        for xx in x..x + w {
            pixels.push(
                src.get_pixel(xx as u32, yy as u32)
                    .0
                    .map(|v| v as f32 / 255.0),
            );
        }
    }
    RgbImage::new(w, h, pixels)
}
#[cfg(test)]
pub(super) fn crop(img: &RgbImage, frac: [f64; 4]) -> Result<RgbImage, String> {
    let (x, y, w, h) = rect(frac, img.width, img.height);
    let mut pixels = Vec::with_capacity(w * h);
    for yy in y..y + h {
        pixels.extend_from_slice(&img.pixels[yy * img.width + x..yy * img.width + x + w]);
    }
    RgbImage::new(w, h, pixels)
}
/// [`super::renderer::Evaluator`] と同じ暗部マスク（輝度の 25% 分位以下）。
pub(super) fn shadow_mask(img: &RgbImage) -> Vec<bool> {
    let gray = img.gray();
    let mut sorted = gray.clone();
    sorted.sort_unstable_by(f32::total_cmp);
    let p = (sorted.len() - 1) as f64 * 0.25;
    let q = sorted[p.floor() as usize] as f64
        + (sorted[p.ceil() as usize] - sorted[p.floor() as usize]) as f64 * p.fract();
    gray.iter().map(|v| *v <= q as f32).collect()
}
fn grad_mean(gray: &[f32], w: usize) -> f64 {
    let mut sum = 0.0;
    for (i, v) in gray.iter().enumerate() {
        let up = gray[if i < w { i } else { i - w }];
        let left = gray[if i % w == 0 { i } else { i - 1 }];
        sum += ((v - up).abs() + (v - left).abs()) as f64;
    }
    sum / gray.len() as f64
}
/// スケール診断値。band_px は画素単位の高域（image_metrics の残差と同じ σ=1.1px）、band_matched は
/// 640 代理の 1.1px と同じ「画面上の周波数」へ σ を長辺比で拡大した高域（σ = 1.1 × long_edge / 640）。
pub(super) fn roi_diagnostics(img: &RgbImage, long_edge: usize) -> Value {
    let gray = img.gray();
    let (w, h) = (img.width, img.height);
    let band = |sigma: f64| {
        let blurred = gaussian(&gray, w, h, sigma);
        std(&gray
            .iter()
            .zip(blurred)
            .map(|(a, b)| a - b)
            .collect::<Vec<_>>())
    };
    let matched_sigma = RESIDUAL_SIGMA * long_edge as f64 / MAX_EDGE as f64;
    json!({
        "grad_mean": round6(grad_mean(&gray, w)),
        "band_px": round6(band(RESIDUAL_SIGMA)),
        "band_matched": round6(band(matched_sigma)),
        "band_matched_sigma": round6(matched_sigma),
        "mean_luma": round6(mean(&gray)),
    })
}
/// natural の 640 代理から ROI を決定的に選ぶ（同じ正規化座標を全スケール・全集合で使う）。
pub(super) fn select_rois(img: &RgbImage) -> Vec<(String, [f64; 4])> {
    let (w, h) = (img.width, img.height);
    if w < 8 || h < 8 {
        return Vec::new();
    }
    let side = ((ROI_FRAC * w.max(h) as f64).round() as usize).clamp(8, w.min(h));
    let stride = (side / 2).max(1);
    let gray = img.gray();
    // (x, y, 平均輝度, 平均勾配)。白飛びが 1% を超えるタイルは平坦候補から外す。
    let mut tiles = Vec::new();
    let mut y = 0;
    while y + side <= h {
        let mut x = 0;
        while x + side <= w {
            let (mut luma, mut grad, mut clipped) = (0.0, 0.0, 0usize);
            for yy in y..y + side {
                for xx in x..x + side {
                    let i = yy * w + xx;
                    luma += gray[i] as f64;
                    if xx > x && yy > y {
                        grad +=
                            ((gray[i] - gray[i - 1]).abs() + (gray[i] - gray[i - w]).abs()) as f64;
                    }
                    clipped += usize::from(img.pixels[i].iter().any(|v| *v >= 0.985));
                }
            }
            let n = (side * side) as f64;
            tiles.push((x, y, luma / n, grad / n, clipped as f64 / n));
            x += stride;
        }
        y += stride;
    }
    let frac = |x: usize, y: usize| {
        [
            x as f64 / w as f64,
            y as f64 / h as f64,
            (x + side) as f64 / w as f64,
            (y + side) as f64 / h as f64,
        ]
    };
    let mut rois = vec![("center".to_string(), frac((w - side) / 2, (h - side) / 2))];
    if let Some(t) = tiles.iter().max_by(|a, b| a.3.total_cmp(&b.3)) {
        rois.push(("detail".into(), frac(t.0, t.1)));
    }
    let mut lumas: Vec<f64> = tiles.iter().map(|t| t.2).collect();
    lumas.sort_by(f64::total_cmp);
    if let Some(q) = lumas.get(lumas.len() / 4) {
        if let Some(t) = tiles
            .iter()
            .filter(|t| t.2 <= *q && t.4 < 0.01)
            .min_by(|a, b| a.3.total_cmp(&b.3))
        {
            rois.push(("flat_dark".into(), frac(t.0, t.1)));
        }
    }
    if let Some(t) = tiles
        .iter()
        .filter(|t| (0.2..=0.8).contains(&t.2) && t.4 < 0.01)
        .min_by(|a, b| a.3.total_cmp(&b.3))
    {
        rois.push(("flat_mid".into(), frac(t.0, t.1)));
    }
    rois
}

/// Shared with the measurement harness; stars are deliberately disabled for ROI crops.
pub(super) fn measure(
    base: &RgbImage,
    image: &RgbImage,
    long_edge: usize,
) -> Result<Value, String> {
    let mask = shadow_mask(base);
    let metrics = super::metrics::image_metrics(
        base,
        image,
        &super::stars::ExifHints::default(),
        false,
        Some(&mask),
    )?;
    Ok(json!({"metrics": metrics, "diag": roi_diagnostics(image, long_edge)}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn selection_is_deterministic_and_coordinates_are_bounded() {
        let img = RgbImage::new(
            640,
            427,
            (0..640 * 427)
                .map(|i| {
                    let (x, y) = (i % 640, i / 640);
                    let v = if x > 400 {
                        ((x + y) % 7) as f32 / 7.0
                    } else {
                        y as f32 / 427.0 * 0.5
                    };
                    [v; 3]
                })
                .collect(),
        )
        .unwrap();
        let rois = select_rois(&img);
        assert_eq!(rois, select_rois(&img));
        assert_eq!(rois.len(), 4);
        for (_, f) in rois {
            let (x, y, w, h) = rect(f, 6000, 4000);
            assert!(x + w <= 6000 && y + h <= 4000 && w >= 700);
            assert!(crop(&img, f).is_ok());
        }
        assert_eq!(rect([1.0, 1.0, 1.0, 1.0], 6000, 4000), (5992, 3992, 8, 8));
        assert_eq!(rect([0.0, 0.0, 1.0, 1.0], 3, 2), (0, 0, 3, 2));
        let tiny = RgbImage::new(3, 2, vec![[0.5; 3]; 6]).unwrap();
        assert!(select_rois(&tiny).is_empty());
    }
    #[test]
    fn ineligible_mid_tones_are_missing_without_substitution() {
        let img = RgbImage::new(64, 48, vec![[0.01; 3]; 64 * 48]).unwrap();
        assert!(!select_rois(&img).iter().any(|r| r.0 == "flat_mid"));
        let m = measure(&img, &img, 6000).unwrap();
        assert_eq!(m["metrics"]["star_count"], 0);
        assert_eq!(m["diag"]["band_matched_sigma"], json!(10.3125));
    }
}
