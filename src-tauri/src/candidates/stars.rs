use super::{
    image_ops::{components, gaussian, mean, round6},
    RgbImage,
};
use serde::{Deserialize, Serialize};

const STAR_MIN_EXPOSURE_SEC: f64 = 2.0;
const STAR_MAX_EV100: f64 = -2.0;
const STAR_MIN_POINT_DENSITY: f64 = 0.0008;
const STAR_NO_EXIF_MIN_DARK_FRACTION: f64 = 0.72;
const STAR_NO_EXIF_MIN_POINT_DENSITY: f64 = 0.002;
const MAX_STAR_BRIGHT_RATIO: f64 = 0.005;
const STAR_MAX_BIG_LIGHT_RATIO: f64 = 0.02;
const STAR_BIG_LIGHT_LEVEL: f32 = 0.85;
const STAR_LOCAL_SIGMA: f64 = 3.0;
const STAR_PEAK_LOW: f32 = 0.04;
const STAR_PEAK_HIGH: f32 = 0.08;
const STAR_POINT_MAX_AREA_RATIO: f64 = 0.00011;
const STAR_POINT_MIN_AREA_CAP: usize = 4;

#[derive(Clone, Copy, Debug, Default, Deserialize)]
pub struct ExifHints {
    pub exposure_time: Option<f64>,
    pub f_number: Option<f64>,
    pub iso: Option<f64>,
}
fn exif_night_state(exif: &ExifHints) -> &'static str {
    let Some(t) = exif.exposure_time.filter(|v| v.is_finite() && *v > 0.0) else {
        return "unknown";
    };
    if t < STAR_MIN_EXPOSURE_SEC {
        return "daylight";
    }
    let (Some(n), Some(iso)) = (exif.f_number, exif.iso) else {
        return "unknown";
    };
    if !n.is_finite() || n <= 0.0 || !iso.is_finite() || iso <= 0.0 {
        return "unknown";
    }
    let ev = (n * n / t).log2() - (iso / 100.0).log2();
    if !ev.is_finite() {
        return "unknown";
    }
    if ev <= STAR_MAX_EV100 {
        "night"
    } else {
        "daylight"
    }
}
#[derive(Debug, Serialize, Deserialize)]
pub struct StarMeasurements {
    pub is_star_scene: bool,
    pub star_count: usize,
    pub star_point_density: Option<f64>,
    pub dark_background_ratio: f64,
    pub bright_point_ratio: f64,
    pub exif_night: bool,
    pub exif_state: String,
    pub star_halo: f64,
    pub star_color_saturation: f64,
}
fn fraction(gray: &[f32], test: impl Fn(f32) -> bool) -> f64 {
    gray.iter().filter(|&&v| test(v)).count() as f64 / gray.len() as f64
}
fn point_light_count(gray: &[f32], w: usize, h: usize) -> usize {
    let blurred = gaussian(gray, w, h, STAR_LOCAL_SIGMA);
    let diff: Vec<f32> = gray.iter().zip(blurred).map(|(a, b)| a - b).collect();
    let max_area = STAR_POINT_MIN_AREA_CAP
        .max((gray.len() as f64 * STAR_POINT_MAX_AREA_RATIO).round_ties_even() as usize);
    components(
        &diff.iter().map(|v| *v >= STAR_PEAK_LOW).collect::<Vec<_>>(),
        w,
        h,
    )
    .iter()
    .filter(|c| {
        c.area <= max_area
            && (c.y..c.y + c.height)
                .any(|y| (c.x..c.x + c.width).any(|x| diff[y * w + x] >= STAR_PEAK_HIGH))
    })
    .count()
}
pub fn star_measurements(
    image: &RgbImage,
    exif: &ExifHints,
    evaluate_gate: bool,
) -> StarMeasurements {
    let gray = image.gray();
    let (w, h) = (image.width, image.height);
    let dark_fraction = fraction(&gray, |v| v < 0.20);
    let mut sorted = gray.clone();
    sorted.sort_unstable_by(f32::total_cmp);
    // numpy percentile の linear interpolation。
    let pos = (sorted.len() - 1) as f64 * 0.9935;
    let lo = pos.floor() as usize;
    let hi = pos.ceil() as usize;
    let threshold = 0.58_f64
        .max(sorted[lo] as f64 + (sorted[hi] as f64 - sorted[lo] as f64) * (pos - lo as f64));
    // numpy は float32 配列と scalar の比較時に scalar を float32 に戻す。
    let bright: Vec<bool> = gray.iter().map(|v| *v >= threshold as f32).collect();
    let bright_ratio = bright.iter().filter(|&&v| v).count() as f64 / gray.len() as f64;
    let max_area = 12.max((gray.len() as f64 * 0.00045) as usize);
    let points: Vec<_> = components(&bright, w, h)
        .into_iter()
        .filter(|c| c.area <= max_area)
        .collect();
    let state = exif_night_state(exif);
    let mut density = None;
    let mut is_star_scene = false;
    if evaluate_gate && state != "daylight" {
        let permitted = if state == "night" {
            fraction(&gray, |v| v >= STAR_BIG_LIGHT_LEVEL) <= STAR_MAX_BIG_LIGHT_RATIO
        } else {
            dark_fraction >= STAR_NO_EXIF_MIN_DARK_FRACTION && bright_ratio <= MAX_STAR_BRIGHT_RATIO
        };
        if permitted {
            let d = point_light_count(&gray, w, h) as f64 / gray.len() as f64;
            density = Some(round6(d));
            is_star_scene = d
                >= if state == "night" {
                    STAR_MIN_POINT_DENSITY
                } else {
                    STAR_NO_EXIF_MIN_POINT_DENSITY
                };
        }
    }
    let (mut halo, mut color_clip) = (0.0, 0.0);
    if (is_star_scene || !evaluate_gate) && !points.is_empty() {
        let (mut rings, mut clips) = (Vec::new(), Vec::new());
        for c in &points {
            let pad = 2.max(c.width.max(c.height) * 2);
            let (left, top, right, bottom) = (
                c.x.saturating_sub(pad),
                c.y.saturating_sub(pad),
                (c.x + c.width + pad).min(w),
                (c.y + c.height + pad).min(h),
            );
            let mut ring = Vec::new();
            for y in top..bottom {
                for x in left..right {
                    if x < c.x || x >= c.x + c.width || y < c.y || y >= c.y + c.height {
                        ring.push(gray[y * w + x]);
                    }
                }
            }
            if !ring.is_empty() {
                rings.push(mean(&ring));
            }
            let mut clipped = 0;
            for y in c.y..c.y + c.height {
                for x in c.x..c.x + c.width {
                    if image.pixels[y * w + x]
                        .iter()
                        .copied()
                        .fold(f32::NEG_INFINITY, f32::max)
                        >= 0.995
                    {
                        clipped += 1;
                    }
                }
            }
            clips.push(clipped as f64 / (c.width * c.height) as f64);
        }
        if !clips.is_empty() {
            color_clip = clips.iter().sum::<f64>() / clips.len() as f64;
        }
        if !rings.is_empty() {
            halo = (rings.iter().sum::<f64>() / rings.len() as f64 / threshold.max(0.01))
                .clamp(0.0, 1.0);
        }
    }
    StarMeasurements {
        is_star_scene,
        star_count: points.len(),
        star_point_density: density,
        dark_background_ratio: round6(dark_fraction),
        bright_point_ratio: round6(bright_ratio),
        exif_night: state == "night",
        exif_state: state.into(),
        star_halo: round6(halo),
        star_color_saturation: round6(color_clip),
    }
}
pub fn detect_star_scene(image: &RgbImage, exif: &ExifHints) -> StarMeasurements {
    star_measurements(image, exif, true)
}
