use super::{
    image_ops::{gaussian, mean, round6, std},
    stars::{star_measurements, ExifHints},
    RgbImage,
};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Metrics {
    pub highlight_clip: f64,
    pub shadow_clip: f64,
    pub noise_flat: f64,
    pub source_noise_flat: f64,
    pub detail: f64,
    pub source_detail: f64,
    pub halo: f64,
    pub color_saturation_clip: f64,
    pub tonal_range: f64,
    pub source_deviation: f64,
    pub shadow_mean: f64,
    pub star_count: usize,
    pub star_halo: f64,
    pub star_color_saturation: f64,
    // Python の応答と同じ別名。
    pub highlight_clipping: f64,
    pub shadow_crushing: f64,
    pub flat_region_noise: f64,
    pub detail_halo: f64,
    pub color_clip: f64,
    pub tone_range: f64,
    pub baseline_deviation: f64,
}
fn gradient(gray: &[f32], w: usize) -> Vec<f32> {
    gray.iter()
        .enumerate()
        .map(|(i, v)| {
            (v - gray[if i < w { i } else { i - w }]).abs()
                + (v - gray[if i % w == 0 { i } else { i - 1 }]).abs()
        })
        .collect()
}
pub fn image_metrics(
    baseline: &RgbImage,
    transformed: &RgbImage,
    exif: &ExifHints,
    star_gate: bool,
    shadow_mask: Option<&[bool]>,
) -> Result<Metrics, String> {
    if baseline.width != transformed.width
        || baseline.height != transformed.height
        || shadow_mask.is_some_and(|m| m.len() != transformed.pixels.len())
    {
        return Err("代理画像または暗部マスクの寸法が一致しません。".into());
    }
    let (w, h) = (transformed.width, transformed.height);
    let gray = transformed.gray();
    let source_gray = baseline.gray();
    // RGB ごとにぼかしてから輝度へ。先に輝度へ変換すると float32 の丸め順が変わる。
    let channels: Vec<Vec<f32>> = (0..3)
        .map(|c| {
            gaussian(
                &transformed.pixels.iter().map(|p| p[c]).collect::<Vec<_>>(),
                w,
                h,
                1.1,
            )
        })
        .collect();
    let residual: Vec<f32> = gray
        .iter()
        .enumerate()
        .map(|(i, v)| {
            v - (channels[0][i] * 0.2126 + channels[1][i] * 0.7152 + channels[2][i] * 0.0722)
        })
        .collect();
    let grad = gradient(&gray, w);
    let source_grad = gradient(&source_gray, w);
    let flat_limit = 0.008_f64.max(mean(&grad) * 1.35);
    let flat: Vec<bool> = grad.iter().map(|v| *v < flat_limit as f32).collect();
    let select = |v: &[f32], mask: &[bool]| -> Vec<f32> {
        v.iter()
            .zip(mask)
            .filter_map(|(v, m)| m.then_some(*v))
            .collect()
    };
    let flat_residual = select(&residual, &flat);
    let flat_source = select(&source_grad, &flat);
    let noise = std(if flat_residual.is_empty() {
        &residual
    } else {
        &flat_residual
    });
    let source_noise = std(if flat_source.is_empty() {
        &source_grad
    } else {
        &flat_source
    });
    let mut high = 0;
    let mut shadow = 0;
    let mut colour = 0;
    for p in &transformed.pixels {
        let max = p.iter().copied().fold(f32::NEG_INFINITY, f32::max);
        let min = p.iter().copied().fold(f32::INFINITY, f32::min);
        high += usize::from(max >= 0.985);
        shadow += usize::from(min <= 0.018);
        colour += usize::from(max >= 0.985 && max - min > 0.55);
    }
    let n = gray.len() as f64;
    let gray_mean = mean(&gray);
    let gray_std = std(&gray);
    let deviation: Vec<f32> = transformed
        .pixels
        .iter()
        .flatten()
        .zip(baseline.pixels.iter().flatten())
        .map(|(a, b)| (a - b).abs())
        .collect();
    let shadows = shadow_mask.map(|m| select(&gray, m)).unwrap_or_default();
    let mut m = Metrics {
        highlight_clip: round6(high as f64 / n),
        shadow_clip: round6(shadow as f64 / n),
        noise_flat: round6(noise.clamp(0.0, 1.0)),
        source_noise_flat: round6(source_noise.clamp(0.0, 1.0)),
        detail: round6((mean(&grad) * 7.0 + std(&grad) * 2.0).clamp(0.0, 1.0)),
        source_detail: round6((mean(&source_grad) * 5.0 + std(&source_grad)).clamp(0.0, 1.0)),
        halo: round6(((std(&residual) - source_noise * 0.35).max(0.0) * 10.0).clamp(0.0, 1.0)),
        color_saturation_clip: round6(colour as f64 / n),
        tonal_range: round6(
            (((gray_mean + 1.65 * gray_std).min(1.0) - (gray_mean - 1.65 * gray_std).max(0.0))
                / 0.78)
                .clamp(0.0, 1.0),
        ),
        source_deviation: round6(mean(&deviation).clamp(0.0, 1.0)),
        shadow_mean: round6(
            if shadows.is_empty() {
                gray_mean
            } else {
                mean(&shadows)
            }
            .clamp(0.0, 1.0),
        ),
        ..Metrics::default()
    };
    if star_gate {
        let s = star_measurements(transformed, exif, false);
        m.star_count = s.star_count;
        m.star_halo = s.star_halo;
        m.star_color_saturation = s.star_color_saturation;
    }
    m.highlight_clipping = m.highlight_clip;
    m.shadow_crushing = m.shadow_clip;
    m.flat_region_noise = m.noise_flat;
    m.detail_halo = m.halo;
    m.color_clip = m.color_saturation_clip;
    m.tone_range = m.tonal_range;
    m.baseline_deviation = m.source_deviation;
    Ok(m)
}
