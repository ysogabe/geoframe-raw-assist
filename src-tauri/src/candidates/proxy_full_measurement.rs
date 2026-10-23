//! #57: Core Image 代理（640 / 1280）と原寸現像で、同じ候補 adjustments の指標を比べる手動計測。
//!
//! 本番のアルゴリズム・閾値には触れない。候補生成は [`generate`]（アプリと同じ経路）、代理の再現像は
//! [`Renderer`]、原寸は [`coreimage::develop`]（`develop_full_coreimage` と同じ経路）を使う。指標は
//! Rust の純粋関数（[`image_metrics`] / [`star_measurements`]）だけで計算し、Python（local_raw）は使わない。
//! 定義と実行手順は `docs/coreimage-proxy-full-measurement.md`。
//!
//! 出力（既定 `work/issue57/proxy-full/`、git 管理外）:
//! - `cases/<key>.json` … `scripts/evaluate_candidates.py --aggregate-only` が読める形
//!   （`raw_metrics` = 原寸 JPEG を面積平均で代理 640 と同寸へ縮小した指標）
//! - `measurements/<key>.json` … 全スケール × 全 adjustments 集合 × ROI の生測定
//! - `provenance.json` / `proxy_full_summary.{json,md}` … 数値と来歴のみ（画像・EXIF 値・GPS なし）
//! - `app_data/<key>/coreimage/full/*.jpg` … 原寸現像の実画像（EXIF 継承あり。コミットしない）
use super::{
    command::{exif_hints, generate},
    image_ops::{gaussian, mean, round6, std},
    metrics::image_metrics,
    pso,
    renderer::{Renderer, ScratchParent, MAX_EDGE},
    stars::{star_measurements, ExifHints},
    RgbImage,
};
use crate::{
    adjustments::normalize_adjustments,
    coreimage::{self, DevelopMode, DevelopSlot},
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

/// `scripts/evaluate_candidates.py` の COMPARE_METRICS と同じ並び。
const COMPARE_METRICS: [&str; 10] = [
    "highlight_clip",
    "shadow_clip",
    "noise_flat",
    "detail",
    "halo",
    "color_saturation_clip",
    "tonal_range",
    "source_deviation",
    "shadow_mean",
    "star_count",
];
/// ROI で比べる指標（[`image_metrics`] の値）と、スケール診断値（[`roi_diagnostics`]）。
const ROI_METRICS: [&str; 3] = ["detail", "noise_flat", "halo"];
const ROI_DIAG: [&str; 3] = ["grad_mean", "band_px", "band_matched"];
/// 2 つ目の代理（#57 コメント: decoder sharpness < 1 は 640 では見えず 1280 以上で効く）。
const EXTRA_EDGE: usize = 1280;
/// 正規化範囲は [0.8, 1.6]（`normalize_adjustments`）。0 は契約外なので下限 0.8 を使う。
const SWEEP_SHARPNESS: [f64; 4] = [0.8, 1.0, 1.47, 1.6];
const SWEEP_NR: [&str; 3] = ["off", "light", "full"];
/// ROI の一辺 = 長辺 × この比（640 で 80px、6000 で 750px）。
const ROI_FRAC: f64 = 0.125;
/// 変化の向きの不一致を数えるときの不感帯（両方の |Δ| がこれを超えたものだけ数える）。
const DIRECTION_EPS: f64 = 1e-4;
/// [`metrics::image_metrics`] の残差ぼかし σ（px）。
const RESIDUAL_SIGMA: f64 = 1.1;
const FULL_TIMEOUT: Duration = Duration::from_secs(180);

#[derive(Clone)]
struct SetDef {
    id: String,
    /// candidate = 本番候補（natural を含む）、sweep = 制御された単一変数掃引。
    kind: &'static str,
    /// 代理: 候補生成が評価したそのままの値（本番の代理経路と同一）。
    proxy: Value,
    /// 原寸: アプリの Tauri コマンドと同じく normalize_adjustments を通した値。
    full: Value,
}

/// 1 スケール × 1 集合の現像結果。frame は全体画像（原寸ネイティブでは None）。
#[derive(Default)]
struct Rendered {
    frame: Option<RgbImage>,
    rois: Vec<RgbImage>,
    /// band_matched の σ を決める画像全体の長辺（ROI だけのスケールでも原寸の長辺）。
    long_edge: usize,
    applied: Value,
    timing: Value,
    extra: Value,
    error: Option<String>,
}

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.trim().is_empty())
}
fn flag(name: &str, default: bool) -> bool {
    env(name).map_or(default, |v| !matches!(v.as_str(), "0" | "false" | "no"))
}
fn sha256_file(path: &Path) -> Result<(String, u64), String> {
    let mut file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let bytes = std::io::copy(&mut file, &mut hasher).map_err(|e| e.to_string())?;
    Ok((hex(&hasher.finalize()), bytes))
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn command_text(program: &str, args: &[&str]) -> Value {
    Command::new(program)
        .args(args)
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| Value::from(String::from_utf8_lossy(&o.stdout).trim().to_string()))
        .unwrap_or(Value::Null)
}

// ---------------------------------------------------------------------------
// 画像の縮小・切り出し・診断値
// ---------------------------------------------------------------------------

/// 面積平均の重み（出力画素 o が覆う入力区間 [o*s, (o+1)*s) の被覆率 / s）。cv2.INTER_AREA と同じ定義。
fn area_weights(src: usize, dst: usize) -> Vec<Vec<(usize, f32)>> {
    let s = src as f64 / dst as f64;
    (0..dst)
        .map(|o| {
            let (a, b) = (o as f64 * s, (o + 1) as f64 * s);
            let mut weights = Vec::new();
            let mut i = a.floor() as usize;
            while (i as f64) < b && i < src {
                let cover = b.min(i as f64 + 1.0) - a.max(i as f64);
                if cover > 1e-12 {
                    weights.push((i, (cover / s) as f32));
                }
                i += 1;
            }
            weights
        })
        .collect()
}
/// 8bit sRGB（符号化値のまま /255）を面積平均で dw×dh へ縮小する。線形化はしない。
fn area_resize(src: &image::RgbImage, dw: usize, dh: usize) -> Result<RgbImage, String> {
    let (sw, sh) = (src.width() as usize, src.height() as usize);
    let (wx, wy) = (area_weights(sw, dw), area_weights(sh, dh));
    let raw = src.as_raw();
    let mut tmp = vec![[0.0_f32; 3]; sh * dw];
    for y in 0..sh {
        for (ox, weights) in wx.iter().enumerate() {
            let mut acc = [0.0_f32; 3];
            for (ix, w) in weights {
                let p = (y * sw + ix) * 3;
                for c in 0..3 {
                    acc[c] += raw[p + c] as f32 / 255.0 * w;
                }
            }
            tmp[y * dw + ox] = acc;
        }
    }
    let mut out = vec![[0.0_f32; 3]; dw * dh];
    for (oy, weights) in wy.iter().enumerate() {
        for ox in 0..dw {
            let mut acc = [0.0_f32; 3];
            for (iy, w) in weights {
                for c in 0..3 {
                    acc[c] += tmp[iy * dw + ox][c] * w;
                }
            }
            // 丸め誤差で 1 をわずかに超えると RgbImage::new が 255 スケールと誤判定する。
            out[oy * dw + ox] = acc.map(|v| v.clamp(0.0, 1.0));
        }
    }
    RgbImage::new(dw, dh, out)
}
/// 正規化座標 [x0, y0, x1, y1] を w×h の画素矩形 (x, y, rw, rh) にする。
fn rect(frac: [f64; 4], w: usize, h: usize) -> (usize, usize, usize, usize) {
    let x0 = ((frac[0] * w as f64).round() as usize).min(w.saturating_sub(8));
    let y0 = ((frac[1] * h as f64).round() as usize).min(h.saturating_sub(8));
    let x1 = ((frac[2] * w as f64).round() as usize).clamp(x0 + 8, w);
    let y1 = ((frac[3] * h as f64).round() as usize).clamp(y0 + 8, h);
    (x0, y0, x1 - x0, y1 - y0)
}
fn crop_u8(src: &image::RgbImage, frac: [f64; 4]) -> Result<RgbImage, String> {
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
fn crop(img: &RgbImage, frac: [f64; 4]) -> Result<RgbImage, String> {
    let (x, y, w, h) = rect(frac, img.width, img.height);
    let mut pixels = Vec::with_capacity(w * h);
    for yy in y..y + h {
        pixels.extend_from_slice(&img.pixels[yy * img.width + x..yy * img.width + x + w]);
    }
    RgbImage::new(w, h, pixels)
}
/// [`super::renderer::Evaluator`] と同じ暗部マスク（輝度の 25% 分位以下）。
fn shadow_mask(img: &RgbImage) -> Vec<bool> {
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
fn roi_diagnostics(img: &RgbImage, long_edge: usize) -> Value {
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
fn select_rois(img: &RgbImage) -> Vec<(String, [f64; 4])> {
    let (w, h) = (img.width, img.height);
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

// ---------------------------------------------------------------------------
// 現像
// ---------------------------------------------------------------------------

fn render_proxy_scale(
    sidecar: &Path,
    input: &Path,
    scratch: &Path,
    edge: usize,
    quality: Option<f64>,
    sets: &[SetDef],
    rois: &[(String, [f64; 4])],
) -> (Vec<Rendered>, Value) {
    let mut renderer = match Renderer::new_with_edge(sidecar, input, scratch, edge) {
        Ok(r) => r,
        Err(e) => {
            let msg = format!("{}: {}", e.code, e.message);
            let failed = sets
                .iter()
                .map(|_| Rendered {
                    error: Some(msg.clone()),
                    ..Rendered::default()
                })
                .collect();
            return (failed, json!({"error": msg}));
        }
    };
    let mut out = Vec::new();
    for set in sets {
        let started = Instant::now();
        let mut r = Rendered::default();
        match renderer.render_with_quality(&set.proxy, quality) {
            Ok(frame) => {
                r.long_edge = frame.width.max(frame.height);
                match rois.iter().map(|(_, f)| crop(&frame, *f)).collect() {
                    Ok(crops) => r.rois = crops,
                    Err(e) => r.error = Some(e),
                }
                r.frame = Some(frame);
                r.applied = renderer.last_applied["post"].clone();
                r.timing = json!({"wall_s": started.elapsed().as_secs_f64()});
            }
            Err(e) => r.error = Some(format!("{}: {}", e.code, e.message)),
        }
        out.push(r);
    }
    let stats = json!({"decodes":renderer.decodes,"renders":renderer.renders,
        "decode_ms":renderer.decode_ms,"render_ms":renderer.render_ms});
    let finished = renderer
        .finish()
        .err()
        .map(|e| format!("{}: {}", e.code, e.message));
    let mut stats = stats;
    stats["finish_error"] = json!(finished);
    (out, stats)
}

/// 原寸を 1 回現像し、640 / 1280 格子へ縮小した画像とネイティブ ROI の 3 つを返す。
fn render_full(
    app_data: &Path,
    sidecar: &Path,
    capture: &str,
    set: &SetDef,
    dims640: (usize, usize),
    dims1280: Option<(usize, usize)>,
    rois: &[(String, [f64; 4])],
) -> [Rendered; 3] {
    let fail = |msg: String| {
        std::array::from_fn(|_| Rendered {
            error: Some(msg.clone()),
            ..Rendered::default()
        })
    };
    let started = Instant::now();
    let result = match coreimage::develop(
        app_data,
        sidecar,
        capture,
        DevelopMode::Full,
        Some(&set.full),
        &DevelopSlot::new(2),
        FULL_TIMEOUT,
    ) {
        Ok(r) => r,
        Err(e) => return fail(format!("{}: {}", e.code, e.message)),
    };
    let wall = started.elapsed().as_secs_f64();
    let image = match image::open(&result.output_path) {
        Ok(i) => i.into_rgb8(),
        Err(e) => return fail(format!("原寸 JPEG を読めません: {e}")),
    };
    let provenance: Value = fs::read_to_string(&result.provenance_path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(Value::Null);
    let long = image.width().max(image.height()) as usize;
    let timing =
        json!({"wall_s": wall, "cli_elapsed_ms": result.elapsed_ms, "cache_hit": result.cache_hit});
    let extra = json!({"width": result.width, "height": result.height,
        "exif_inherited": result.exif_inherited, "applied_full": provenance["adjustments_applied"]});
    let applied = provenance["adjustments_applied"]["post"].clone();
    let down = |dims: (usize, usize)| -> Rendered {
        let mut r = Rendered {
            long_edge: dims.0.max(dims.1),
            applied: applied.clone(),
            timing: timing.clone(),
            ..Rendered::default()
        };
        match area_resize(&image, dims.0, dims.1).and_then(|frame| {
            let crops = rois
                .iter()
                .map(|(_, f)| crop(&frame, *f))
                .collect::<Result<Vec<_>, _>>()?;
            Ok((frame, crops))
        }) {
            Ok((frame, crops)) => {
                r.frame = Some(frame);
                r.rois = crops;
            }
            Err(e) => r.error = Some(e),
        }
        r
    };
    let d640 = down(dims640);
    let d1280 = dims1280.map(down).unwrap_or_else(|| Rendered {
        error: Some("1280 代理が無いため縮小格子を決められません".into()),
        ..Rendered::default()
    });
    let mut native = Rendered {
        long_edge: long,
        applied: applied.clone(),
        timing,
        extra,
        ..Rendered::default()
    };
    match rois.iter().map(|(_, f)| crop_u8(&image, *f)).collect() {
        Ok(crops) => native.rois = crops,
        Err(e) => native.error = Some(e),
    }
    [d640, d1280, native]
}

// ---------------------------------------------------------------------------
// 指標
// ---------------------------------------------------------------------------

fn gate(id: &str, natural_star: bool) -> bool {
    // 候補生成・evaluate_candidates.py と同じ: natural は星判定、それ以外は stars のみ。
    if id == "natural" {
        natural_star
    } else {
        id == "stars"
    }
}
fn ungated(img: &RgbImage, exif: &ExifHints) -> Value {
    let s = star_measurements(img, exif, false);
    json!({"star_count":s.star_count,"star_halo":s.star_halo,
        "star_color_saturation":s.star_color_saturation,"is_star_scene":s.is_star_scene})
}
/// 1 スケールの全集合を、そのスケールの natural（集合 0）を基準・暗部マスクにして測る。
fn measure_scale(
    rendered: &[Rendered],
    sets: &[SetDef],
    rois: &[(String, [f64; 4])],
    natural_star: bool,
    exif: &ExifHints,
    errors: &mut Vec<Value>,
    scale: &str,
) -> Value {
    let natural = &rendered[0];
    if natural.frame.is_none() && natural.rois.is_empty() && natural.error.is_none() {
        errors
            .push(json!({"scale": scale, "set": "natural", "error": "natural の画像がありません"}));
    }
    let frame_mask = natural.frame.as_ref().map(shadow_mask);
    let roi_masks: Vec<Vec<bool>> = natural.rois.iter().map(shadow_mask).collect();
    let mut entries = Map::new();
    for (set, r) in sets.iter().zip(rendered) {
        let mut entry = json!({"applied": r.applied, "timing": r.timing, "extra": r.extra});
        if let Some(e) = &r.error {
            errors.push(json!({"scale": scale, "set": set.id, "error": e}));
            entry["error"] = json!(e);
            entries.insert(set.id.clone(), entry);
            continue;
        }
        if let (Some(frame), Some(base), Some(mask)) =
            (&r.frame, &natural.frame, frame_mask.as_deref())
        {
            entry["dims"] = json!([frame.width, frame.height]);
            match image_metrics(base, frame, exif, gate(&set.id, natural_star), Some(mask)) {
                Ok(m) => entry["metrics"] = serde_json::to_value(m).unwrap(),
                Err(e) => errors.push(json!({"scale": scale, "set": set.id, "error": e})),
            }
            entry["star_ungated"] = ungated(frame, exif);
        }
        let mut roi_entries = Map::new();
        for (i, (name, frac)) in rois.iter().enumerate() {
            let (Some(img), Some(base), Some(mask)) =
                (r.rois.get(i), natural.rois.get(i), roi_masks.get(i))
            else {
                continue;
            };
            let metrics = match image_metrics(base, img, exif, false, Some(mask)) {
                Ok(m) => serde_json::to_value(m).unwrap(),
                Err(e) => {
                    errors.push(json!({"scale": scale, "set": set.id, "roi": name, "error": e}));
                    json!({"error": e})
                }
            };
            roi_entries.insert(
                name.clone(),
                json!({"frac": frac, "dims": [img.width, img.height], "metrics": metrics,
                    "diag": roi_diagnostics(img, r.long_edge)}),
            );
        }
        entry["rois"] = Value::Object(roi_entries);
        entries.insert(set.id.clone(), entry);
    }
    Value::Object(entries)
}

// ---------------------------------------------------------------------------
// ケース
// ---------------------------------------------------------------------------

struct CaseInput<'a> {
    root_key: &'a str,
    root: &'a Path,
    capture: &'a str,
    preset: &'a str,
}

fn run_case(
    c: &CaseInput,
    sidecar: &Path,
    out: &Path,
    budget: usize,
    seed_override: Option<u32>,
    extra_edge: bool,
    sweeps: bool,
) -> Result<(Value, Value), String> {
    let key = format!("{}__{}__{}", c.root_key, c.capture, c.preset);
    let input = coreimage::resolve_arw(c.root, c.capture).map_err(|e| e.message)?;
    let (arw_sha256, arw_bytes) = sha256_file(&input)?;
    // 星判定（exif_hints: 拡張子の大小を問わない JPG）と原寸の EXIF 継承（`<ID>.JPG`）が読むペア JPG。
    // ファイル名・ハッシュ・サイズだけを記録し、EXIF の値は残さない。
    let mut paired_jpg = Vec::new();
    for entry in fs::read_dir(c.root).map_err(|e| e.to_string())?.flatten() {
        let path = entry.path();
        if path
            .file_stem()
            .is_some_and(|s| s.to_string_lossy() == c.capture)
            && path
                .extension()
                .is_some_and(|x| x.eq_ignore_ascii_case("jpg"))
        {
            let (sha, bytes) = sha256_file(&path)?;
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            paired_jpg.push(json!({"file_name": name, "sha256": sha, "bytes": bytes,
                "used_for_full_exif_inherit": name == format!("{}.JPG", c.capture)}));
        }
    }
    let exif = exif_hints(c.root, c.capture);
    let scratch = out.join("scratch");
    fs::create_dir_all(&scratch).map_err(|e| e.to_string())?;
    let seed = seed_override.unwrap_or_else(|| pso::default_seed(c.capture, c.preset));

    // 1. 現在の本番経路で候補を生成（seed / budget は明示して記録する）。
    let started = Instant::now();
    let generated = generate(
        c.root,
        sidecar,
        ScratchParent::for_directory(&scratch).map_err(|e| e.message)?,
        c.capture,
        c.preset,
        Some(seed),
        Some(budget),
        |_, _, _| {},
    )
    .map_err(|e| format!("{}: {}", e.code, e.message))?;
    let gen_elapsed = started.elapsed().as_secs_f64();
    let natural_star = generated["star_scene"]["is_star_scene"] == json!(true);

    // 2. 測る adjustments 集合: 本番候補（natural が先頭）+ natural 基準の単一変数掃引。
    let candidates = generated["candidates"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let base = candidates
        .iter()
        .find(|v| v["id"] == "natural")
        .map(|v| v["adjustments"].clone())
        .ok_or("natural 候補がありません")?;
    let mut raw_sets: Vec<(String, &'static str, Value)> = candidates
        .iter()
        .map(|v| {
            (
                v["id"].as_str().unwrap_or("?").to_string(),
                "candidate",
                v["adjustments"].clone(),
            )
        })
        .collect();
    if sweeps {
        for s in SWEEP_SHARPNESS {
            let mut a = base.clone();
            a["sharpness"] = json!(s);
            raw_sets.push((format!("sweep_sharpness_{s:.2}"), "sweep", a));
        }
        for nr in SWEEP_NR {
            let mut a = base.clone();
            a["noise_reduction"] = json!(nr);
            raw_sets.push((format!("sweep_nr_{nr}"), "sweep", a));
        }
    }
    let mut sets = Vec::new();
    for (id, kind, proxy) in raw_sets {
        let full = normalize_adjustments(&proxy).map_err(|e| format!("{id}: {}", e.message))?;
        sets.push(SetDef {
            id,
            kind,
            proxy,
            full,
        });
    }

    // 3. 代理 640（本番と同じ jpeg_quality 1.0）。ROI は natural の 640 代理で決める。
    let mut errors = Vec::new();
    let mut scales: BTreeMap<&str, (Vec<Rendered>, Value)> = BTreeMap::new();
    let (p640, stats640) =
        render_proxy_scale(sidecar, &input, &scratch, MAX_EDGE, Some(1.0), &sets, &[]);
    let natural640 = p640[0]
        .frame
        .as_ref()
        .ok_or_else(|| format!("natural の 640 代理が失敗: {:?}", p640[0].error))?;
    let rois = select_rois(natural640);
    let dims640 = (natural640.width, natural640.height);
    let p640: Vec<Rendered> = p640
        .into_iter()
        .map(|mut r| {
            if let Some(f) = &r.frame {
                match rois.iter().map(|(_, fr)| crop(f, *fr)).collect() {
                    Ok(crops) => r.rois = crops,
                    Err(e) => r.error = Some(e),
                }
            }
            r
        })
        .collect();
    scales.insert("proxy640", (p640, stats640));
    // 対照: 同じ 640 代理を原寸と同じ CLI 既定の JPEG 品質で書く（品質の寄与を分ける）。
    scales.insert(
        "proxy640_qcli",
        render_proxy_scale(sidecar, &input, &scratch, MAX_EDGE, None, &sets, &rois),
    );
    let mut dims1280 = None;
    if extra_edge {
        let p1280 = render_proxy_scale(
            sidecar,
            &input,
            &scratch,
            EXTRA_EDGE,
            Some(1.0),
            &sets,
            &rois,
        );
        dims1280 = p1280.0[0].frame.as_ref().map(|f| (f.width, f.height));
        scales.insert("proxy1280", p1280);
    }

    // 4. 原寸（アプリの develop_full_coreimage と同じ develop 経路）。ケースごとの app_data に隔離する。
    let app_data = out.join("app_data").join(&key);
    fs::create_dir_all(&app_data).map_err(|e| e.to_string())?;
    fs::write(
        app_data.join("config.json"),
        json!({"config_version": 1, "source_root": c.root}).to_string(),
    )
    .map_err(|e| e.to_string())?;
    let (mut d640, mut d1280, mut native) = (Vec::new(), Vec::new(), Vec::new());
    for set in &sets {
        let [a, b, n] = render_full(&app_data, sidecar, c.capture, set, dims640, dims1280, &rois);
        d640.push(a);
        d1280.push(b);
        native.push(n);
    }
    scales.insert("full_down640", (d640, Value::Null));
    if extra_edge {
        scales.insert("full_down1280", (d1280, Value::Null));
    }
    scales.insert("full_native", (native, Value::Null));

    // 5. 指標（各スケールの natural を基準にする）。
    let mut measured = Map::new();
    let mut stats = Map::new();
    for (name, (rendered, st)) in &scales {
        measured.insert(
            name.to_string(),
            measure_scale(
                rendered,
                &sets,
                &rois,
                natural_star,
                &exif,
                &mut errors,
                name,
            ),
        );
        if let Some(e) = st["finish_error"].as_str().or(st["error"].as_str()) {
            errors.push(json!({"scale": name, "set": null, "error": format!("renderer: {e}")}));
        }
        stats.insert(name.to_string(), st.clone());
    }

    // 6. 再現性: 640 代理の再現像の指標が、候補生成時の指標と一致するか。
    let mut reproducibility = Map::new();
    for c in &candidates {
        let id = c["id"].as_str().unwrap_or("?");
        let again = &measured["proxy640"][id]["metrics"];
        let diff = COMPARE_METRICS
            .iter()
            .filter_map(|m| Some((c["metrics"][m].as_f64()? - again[m].as_f64()?).abs()))
            .fold(None, |acc: Option<f64>, d| {
                Some(acc.map_or(d, |a| a.max(d)))
            });
        reproducibility.insert(id.into(), json!(diff));
    }

    // 7. evaluate_candidates.py --aggregate-only 互換の case レコード。
    let full640 = &measured["full_down640"];
    let case_candidates: Vec<Value> = candidates
        .iter()
        .map(|cand| {
            let mut cand = cand.clone();
            let id = cand["id"].as_str().unwrap_or("?").to_string();
            let entry = &full640[&id];
            if let Some(e) = entry.get("error") {
                cand["raw_error"] = e.clone();
            } else if !entry["metrics"].is_null() {
                cand["raw_metrics"] = entry["metrics"].clone();
                cand["raw_star_ungated"] = entry["star_ungated"].clone();
                let ms = &entry["timing"]["cli_elapsed_ms"];
                cand["raw_timing"] = json!({
                    "t_total": ms["total"].as_f64().map(|v| v / 1000.0),
                    "t_decode": ms["decode"].as_f64().map(|v| v / 1000.0),
                    "wall_s": entry["timing"]["wall_s"],
                    "engine": "coreimage_full",
                });
            }
            cand
        })
        .collect();
    let record = json!({
        "key": key, "root": c.root_key, "capture_id": c.capture, "preset": c.preset,
        "gen_elapsed": gen_elapsed, "proxy": generated["proxy"], "star_scene": generated["star_scene"],
        "candidates": case_candidates, "rejected": generated["rejected"],
        "seed": generated["seed"], "budget": generated["budget"],
        "raw_engine": "coreimage_full_area640",
    });
    let measurement = json!({
        "key": key, "root": c.root_key, "capture_id": c.capture, "preset": c.preset,
        "source": {"arw_file_name": input.file_name().map(|n| n.to_string_lossy().into_owned()),
            "arw_sha256": arw_sha256, "arw_bytes": arw_bytes, "paired_jpg": paired_jpg},
        "seed": seed, "seed_source": if seed_override.is_some() {"env"} else {"pso::default_seed"},
        "budget": budget, "gen_elapsed": gen_elapsed, "natural_star_scene": natural_star,
        "sets": sets.iter().map(|s| json!({"id": s.id, "kind": s.kind, "adjustments_proxy": s.proxy,
            "adjustments_full": s.full,
            "normalization_changed": coreimage::canonical_json(&s.proxy) != coreimage::canonical_json(&s.full)})).collect::<Vec<_>>(),
        "rois": rois.iter().map(|(n, f)| json!({"name": n, "frac": f})).collect::<Vec<_>>(),
        "renderer_stats": stats,
        "scales": measured,
        "reproducibility_max_abs_diff_vs_generation": reproducibility,
        "errors": errors,
    });
    Ok((record, measurement))
}

// ---------------------------------------------------------------------------
// 集計
// ---------------------------------------------------------------------------

fn ranks(v: &[f64]) -> Vec<f64> {
    let mut idx: Vec<usize> = (0..v.len()).collect();
    idx.sort_by(|a, b| v[*a].total_cmp(&v[*b]));
    let mut r = vec![0.0; v.len()];
    let mut i = 0;
    while i < idx.len() {
        let mut j = i;
        while j + 1 < idx.len() && v[idx[j + 1]] == v[idx[i]] {
            j += 1;
        }
        for k in i..=j {
            r[idx[k]] = (i + j) as f64 / 2.0 + 1.0;
        }
        i = j + 1;
    }
    r
}
fn pearson(x: &[f64], y: &[f64]) -> Option<f64> {
    if x.len() < 3 || x.len() != y.len() {
        return None;
    }
    let (mx, my) = (
        x.iter().sum::<f64>() / x.len() as f64,
        y.iter().sum::<f64>() / y.len() as f64,
    );
    let (mut sxy, mut sxx, mut syy) = (0.0, 0.0, 0.0);
    for (a, b) in x.iter().zip(y) {
        sxy += (a - mx) * (b - my);
        sxx += (a - mx).powi(2);
        syy += (b - my).powi(2);
    }
    (sxx > 0.0 && syy > 0.0).then(|| sxy / (sxx * syy).sqrt())
}
fn spearman(x: &[f64], y: &[f64]) -> Option<f64> {
    pearson(&ranks(x), &ranks(y))
}
fn avg(v: &[f64]) -> Option<f64> {
    (!v.is_empty()).then(|| v.iter().sum::<f64>() / v.len() as f64)
}
fn r6(v: Option<f64>) -> Value {
    v.map_or(Value::Null, |v| json!(round6(v)))
}
/// 2 系列（a = 基準側, b = 比較側）の一致度。
fn pair_stats(a: &[f64], b: &[f64]) -> Value {
    let diffs: Vec<f64> = a.iter().zip(b).map(|(a, b)| b - a).collect();
    let abs: Vec<f64> = diffs.iter().map(|d| d.abs()).collect();
    let disagree = a
        .iter()
        .zip(b)
        .filter(|(a, b)| {
            a.abs() > DIRECTION_EPS && b.abs() > DIRECTION_EPS && a.signum() != b.signum()
        })
        .count();
    let one_sided = a
        .iter()
        .zip(b)
        .filter(|(a, b)| (a.abs() > DIRECTION_EPS) != (b.abs() > DIRECTION_EPS))
        .count();
    json!({"n": a.len(), "bias_b_minus_a": r6(avg(&diffs)), "mae": r6(avg(&abs)),
        "pearson": r6(pearson(a, b)), "spearman": r6(spearman(a, b)),
        "mean_a": r6(avg(a)), "mean_b": r6(avg(b)),
        "direction_disagree": disagree, "one_sided_change": one_sided})
}
fn metric(entry: &Value, path: &[&str], name: &str) -> Option<f64> {
    let mut v = entry;
    for p in path {
        v = v.get(*p)?;
    }
    v.get(name)?.as_f64()
}

const FRAME_PAIRS: [(&str, &str); 5] = [
    ("proxy640", "full_down640"),
    ("proxy1280", "full_down1280"),
    ("proxy640", "proxy1280"),
    ("proxy640", "proxy640_qcli"),
    ("full_down640", "full_down1280"),
];
const ROI_PAIRS: [(&str, &str); 3] = [
    ("proxy640", "full_native"),
    ("proxy1280", "full_native"),
    ("full_down640", "full_native"),
];
const SWEEP_SCALES: [&str; 5] = [
    "proxy640",
    "proxy1280",
    "full_down640",
    "full_down1280",
    "full_native",
];

fn summarize(measurements: &[Value]) -> Value {
    let ids = |m: &Value, kind: &str| -> Vec<String> {
        m["sets"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|s| s["kind"] == kind)
            .filter_map(|s| s["id"].as_str().map(String::from))
            .collect()
    };
    // 全体指標: 絶対値（natural を含む）と natural 比 Δ（natural 以外）。
    let mut frame = Map::new();
    for (sa, sb) in FRAME_PAIRS {
        let mut per_metric = Map::new();
        for name in COMPARE_METRICS {
            let (mut va, mut vb, mut da, mut db, mut within) =
                (vec![], vec![], vec![], vec![], vec![]);
            for m in measurements {
                let (a, b) = (&m["scales"][sa], &m["scales"][sb]);
                let (Some(na), Some(nb)) = (
                    metric(&a["natural"], &["metrics"], name),
                    metric(&b["natural"], &["metrics"], name),
                ) else {
                    continue;
                };
                let (mut ca, mut cb) = (vec![], vec![]);
                for id in ids(m, "candidate") {
                    let (Some(x), Some(y)) = (
                        metric(&a[&id], &["metrics"], name),
                        metric(&b[&id], &["metrics"], name),
                    ) else {
                        continue;
                    };
                    ca.push(x);
                    cb.push(y);
                    if id != "natural" {
                        da.push(x - na);
                        db.push(y - nb);
                    }
                }
                if let Some(rho) = spearman(&ca, &cb) {
                    within.push(rho);
                }
                va.extend(ca);
                vb.extend(cb);
            }
            per_metric.insert(
                name.into(),
                json!({"absolute": pair_stats(&va, &vb), "delta_vs_natural": pair_stats(&da, &db),
                    "within_case_spearman_mean": r6(avg(&within)), "within_case_n": within.len()}),
            );
        }
        frame.insert(format!("{sa}__vs__{sb}"), Value::Object(per_metric));
    }
    // ROI: スケール間で絶対値は比べられないので natural 比 Δ の向き・相関だけを比べる。
    let mut roi = Map::new();
    for (sa, sb) in ROI_PAIRS {
        let mut per_roi = Map::new();
        for roi_name in ["center", "detail", "flat_dark", "flat_mid"] {
            let mut per_metric = Map::new();
            for (path, names) in [("metrics", &ROI_METRICS[..]), ("diag", &ROI_DIAG[..])] {
                for name in names {
                    for kind in ["candidate", "sweep"] {
                        let (mut da, mut db) = (vec![], vec![]);
                        for m in measurements {
                            let (a, b) = (&m["scales"][sa], &m["scales"][sb]);
                            let roi_path = ["rois", roi_name, path];
                            let (Some(na), Some(nb)) = (
                                metric(&a["natural"], &roi_path, name),
                                metric(&b["natural"], &roi_path, name),
                            ) else {
                                continue;
                            };
                            for id in ids(m, kind) {
                                if id == "natural" {
                                    continue;
                                }
                                if let (Some(x), Some(y)) = (
                                    metric(&a[&id], &roi_path, name),
                                    metric(&b[&id], &roi_path, name),
                                ) {
                                    da.push(x - na);
                                    db.push(y - nb);
                                }
                            }
                        }
                        per_metric.insert(format!("{path}.{name}.{kind}"), pair_stats(&da, &db));
                    }
                }
            }
            per_roi.insert(roi_name.into(), Value::Object(per_metric));
        }
        roi.insert(format!("{sa}__vs__{sb}"), Value::Object(per_roi));
    }
    // 掃引: スケールごとの natural 比 Δ の平均（ケース平均）。
    let mut sweep = Map::new();
    let sweep_ids: Vec<String> = measurements
        .first()
        .map(|m| ids(m, "sweep"))
        .unwrap_or_default();
    for id in &sweep_ids {
        let mut per_scale = Map::new();
        for scale in SWEEP_SCALES {
            let mut row = Map::new();
            let mut push = |label: String, path: &[&str], name: &str| {
                let d: Vec<f64> = measurements
                    .iter()
                    .filter_map(|m| {
                        let s = &m["scales"][scale];
                        Some(
                            metric(&s[id.as_str()], path, name)?
                                - metric(&s["natural"], path, name)?,
                        )
                    })
                    .collect();
                row.insert(label, json!({"n": d.len(), "mean_delta": r6(avg(&d))}));
            };
            for name in ["detail", "noise_flat", "halo", "source_deviation"] {
                push(format!("frame.{name}"), &["metrics"], name);
            }
            for roi_name in ["detail", "flat_dark", "flat_mid"] {
                for name in ["band_px", "band_matched"] {
                    push(
                        format!("roi.{roi_name}.{name}"),
                        &["rois", roi_name, "diag"],
                        name,
                    );
                }
                for name in ["noise_flat", "halo"] {
                    push(
                        format!("roi.{roi_name}.{name}"),
                        &["rois", roi_name, "metrics"],
                        name,
                    );
                }
            }
            per_scale.insert(scale.into(), Value::Object(row));
        }
        sweep.insert(id.clone(), Value::Object(per_scale));
    }
    let errors: usize = measurements
        .iter()
        .map(|m| m["errors"].as_array().map_or(0, Vec::len))
        .sum();
    let repro: Vec<f64> = measurements
        .iter()
        .flat_map(|m| {
            m["reproducibility_max_abs_diff_vs_generation"]
                .as_object()
                .into_iter()
                .flat_map(|o| o.values().filter_map(Value::as_f64).collect::<Vec<_>>())
        })
        .collect();
    json!({
        "cases": measurements.len(),
        "render_errors": errors,
        "direction_eps": DIRECTION_EPS,
        "proxy640_rerender_max_abs_diff_vs_generation": r6(repro.iter().copied().reduce(f64::max)),
        "frame": frame, "roi": roi, "sweep": sweep,
    })
}

fn fmt(v: &Value) -> String {
    match v.as_f64() {
        Some(x) if v.is_f64() => format!("{x:+.4}"),
        Some(x) => format!("{x}"),
        None => "-".into(),
    }
}
fn render_markdown(summary: &Value, provenance: &Value) -> String {
    let mut s = String::from("# Core Image 代理 vs 原寸 計測サマリ（#57）\n\n");
    s += &format!(
        "- commit: {} / CLI {} / macOS {}\n- ケース: {} / 描画エラー: {} / 640 再現像の生成時指標との最大差: {}\n- 向き不一致の不感帯: |Δ| > {}\n\n",
        provenance["git"]["head"].as_str().unwrap_or("?"),
        provenance["sidecar"]["cli_version"].as_str().unwrap_or("?"),
        provenance["sidecar"]["macos_version"].as_str().unwrap_or("?"),
        summary["cases"], summary["render_errors"],
        fmt(&summary["proxy640_rerender_max_abs_diff_vs_generation"]), DIRECTION_EPS,
    );
    for (pair, rows) in summary["frame"].as_object().into_iter().flatten() {
        s += &format!("## 全体指標 {pair}（a=左, b=右）\n\n| 指標 | n | bias(b−a) | MAE | ρ写真内 | Δn | Δbias | Δpearson | Δspearman | 向き不一致 | 片側のみ変化 |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n");
        for name in COMPARE_METRICS {
            let r = &rows[name];
            let (a, d) = (&r["absolute"], &r["delta_vs_natural"]);
            s += &format!(
                "| {name} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |\n",
                a["n"],
                fmt(&a["bias_b_minus_a"]),
                fmt(&a["mae"]),
                fmt(&r["within_case_spearman_mean"]),
                d["n"],
                fmt(&d["bias_b_minus_a"]),
                fmt(&d["pearson"]),
                fmt(&d["spearman"]),
                d["direction_disagree"],
                d["one_sided_change"],
            );
        }
        s += "\n";
    }
    for (pair, rois) in summary["roi"].as_object().into_iter().flatten() {
        s += &format!("## ROI natural 比 Δ {pair}\n\n| ROI | 指標 | 集合 | n | mean Δa | mean Δb | pearson | 向き不一致 | 片側のみ |\n|---|---|---|---:|---:|---:|---:|---:|---:|\n");
        for (roi, rows) in rois.as_object().into_iter().flatten() {
            for (name, r) in rows.as_object().into_iter().flatten() {
                if r["n"] == json!(0) {
                    continue;
                }
                let (path, kind) = name.rsplit_once('.').unwrap_or((name, ""));
                s += &format!(
                    "| {roi} | {path} | {kind} | {} | {} | {} | {} | {} | {} |\n",
                    r["n"],
                    fmt(&r["mean_a"]),
                    fmt(&r["mean_b"]),
                    fmt(&r["pearson"]),
                    r["direction_disagree"],
                    r["one_sided_change"],
                );
            }
        }
        s += "\n";
    }
    s += "## 掃引（natural 比 Δ のケース平均）\n\n";
    for (id, scales) in summary["sweep"].as_object().into_iter().flatten() {
        s += &format!("### {id}\n\n| 量 |");
        for scale in SWEEP_SCALES {
            s += &format!(" {scale} |");
        }
        s += "\n|---|";
        s += &"---:|".repeat(SWEEP_SCALES.len());
        s += "\n";
        let labels: Vec<String> = scales[SWEEP_SCALES[0]]
            .as_object()
            .map(|o| o.keys().cloned().collect())
            .unwrap_or_default();
        for label in labels {
            s += &format!("| {label} |");
            for scale in SWEEP_SCALES {
                s += &format!(" {} |", fmt(&scales[scale][&label]["mean_delta"]));
            }
            s += "\n";
        }
        s += "\n";
    }
    s
}

// ---------------------------------------------------------------------------
// エントリポイント
// ---------------------------------------------------------------------------

fn write_json(path: &Path, value: &Value) {
    fs::write(path, serde_json::to_string_pretty(value).unwrap() + "\n").unwrap();
}

/// 手動計測（release sidecar 必須。手順は docs/coreimage-proxy-full-measurement.md）:
/// GEOFRAME_PSO_RAW_ROOT=<stars の RAW> GEOFRAME_PSO_SIDECAR=<CLI> \
///   cargo test --release --manifest-path src-tauri/Cargo.toml \
///   coreimage_proxy_full_measurement -- --ignored --nocapture
#[test]
#[ignore]
fn coreimage_proxy_full_measurement() {
    assert!(!cfg!(debug_assertions), "--release で実行してください");
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let out = env("GEOFRAME_PROXY_MEASURE_OUT")
        .map(PathBuf::from)
        .unwrap_or_else(|| repo.join("work/issue57/proxy-full"));
    let out_children = ["cases", "measurements", "app_data", "scratch"];
    let aggregate_only = flag("GEOFRAME_PROXY_MEASURE_AGGREGATE_ONLY", false);
    if aggregate_only {
        let mut measurements: Vec<Value> = fs::read_dir(out.join("measurements"))
            .expect("measurements/ がありません")
            .flatten()
            .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
            .map(|e| serde_json::from_str(&fs::read_to_string(e.path()).unwrap()).unwrap())
            .collect();
        measurements.sort_by(|a, b| a["key"].as_str().cmp(&b["key"].as_str()));
        let provenance: Value = fs::read_to_string(out.join("provenance.json"))
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or(Value::Null);
        let summary = summarize(&measurements);
        write_json(&out.join("proxy_full_summary.json"), &summary);
        fs::write(
            out.join("proxy_full_summary.md"),
            render_markdown(&summary, &provenance),
        )
        .unwrap();
        println!("再集計: {} ケース → {}", measurements.len(), out.display());
        return;
    }
    let stars_root =
        Path::new(&env("GEOFRAME_PSO_RAW_ROOT").expect("GEOFRAME_PSO_RAW_ROOT を指定してください"))
            .canonicalize()
            .unwrap();
    let image_work_root = env("GEOFRAME_PROXY_MEASURE_IMAGE_WORK_ROOT")
        .map(|p| Path::new(&p).canonicalize().unwrap());
    let sidecar = env("GEOFRAME_PSO_SIDECAR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            repo.join("src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin")
        });
    let budget: usize = env("GEOFRAME_PROXY_MEASURE_BUDGET").map_or(48, |v| v.parse().unwrap());
    let seed_override: Option<u32> = env("GEOFRAME_PROXY_MEASURE_SEED").map(|v| v.parse().unwrap());
    let extra_edge = flag("GEOFRAME_PROXY_MEASURE_1280", true);
    let sweeps = flag("GEOFRAME_PROXY_MEASURE_SWEEP", true);
    // "3" なら先頭 3 件、"DSC07402,DSC07404" なら撮影 ID で絞る。
    let filter = env("GEOFRAME_PROXY_MEASURE_CASES");

    if out_children.iter().any(|c| out.join(c).exists()) {
        assert!(
            flag("GEOFRAME_PROXY_MEASURE_FORCE", false),
            "{} に前回の出力があります。GEOFRAME_PROXY_MEASURE_FORCE=1 で消して再実行します",
            out.display()
        );
        for c in out_children {
            let path = out.join(c);
            if path.exists() {
                fs::remove_dir_all(&path)
                    .unwrap_or_else(|e| panic!("{} を消せません: {e}", path.display()));
            }
        }
    }
    for c in ["cases", "measurements"] {
        fs::create_dir_all(out.join(c)).unwrap();
    }

    let manifest_text = include_str!("../../../scripts/eval_cases/candidates_baseline.json");
    let manifest: Value = serde_json::from_str(manifest_text).unwrap();
    let mut cases = Vec::new();
    let mut pending = Vec::new();
    for set in manifest["sets"].as_array().unwrap() {
        let root_key = set["root"].as_str().unwrap();
        let root = match root_key {
            "stars" => Some(stars_root.as_path()),
            "image_work" => image_work_root.as_deref(),
            _ => None,
        };
        for case in set["cases"].as_array().unwrap() {
            let (capture, preset) = case.as_str().unwrap().split_once(':').unwrap();
            match root {
                Some(root) => cases.push((root_key, root, capture, preset)),
                None => pending.push(
                    json!({"root": root_key, "capture_id": capture, "preset": preset,
                    "status": "pending_source_materials"}),
                ),
            }
        }
    }
    if let Some(f) = &filter {
        if let Ok(n) = f.parse::<usize>() {
            cases.truncate(n);
        } else {
            let wanted: Vec<&str> = f.split(',').map(str::trim).collect();
            cases.retain(|c| wanted.contains(&c.2));
        }
    }

    assert!(
        !cases.is_empty(),
        "計測するケースが 0 件です（GEOFRAME_PROXY_MEASURE_CASES を確認）"
    );
    let version = coreimage::cli_version(&sidecar).expect("CLI --version");
    let mut provenance = json!({
        "issue": 57,
        "generated_at_unix": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs(),
        "git": {"head": command_text("git", &["-C", repo.to_str().unwrap(), "rev-parse", "HEAD"]),
            "branch": command_text("git", &["-C", repo.to_str().unwrap(), "rev-parse", "--abbrev-ref", "HEAD"]),
            "status_porcelain": command_text("git", &["-C", repo.to_str().unwrap(), "status", "--porcelain"])},
        "cargo_pkg_version": env!("CARGO_PKG_VERSION"),
        "rustc": command_text("rustc", &["-V"]),
        "os": {"product_version": command_text("sw_vers", &["-productVersion"]),
            "build_version": command_text("sw_vers", &["-buildVersion"]), "arch": std::env::consts::ARCH},
        // 絶対パスは残さない（ファイル名と内容ハッシュのみ）。
        "sidecar": {"file_name": sidecar.file_name().map(|n| n.to_string_lossy().into_owned()),
            "cli_version": version.cli_version, "macos_version": version.macos_version,
            "binary_sha256": version.binary_sha256},
        "manifest": {"path": "scripts/eval_cases/candidates_baseline.json",
            "sha256": hex(&Sha256::digest(manifest_text.as_bytes()))},
        "settings": {
            "budget": budget,
            "seed": seed_override.map_or(json!("pso::default_seed(capture, preset)"), |s| json!(s)),
            "proxy": {"mode": "preview", "decoder_version": "9", "profile": "apple_default",
                "lens_correction": true, "edges": if extra_edge { json!([MAX_EDGE, EXTRA_EDGE]) } else { json!([MAX_EDGE]) },
                "jpeg_quality": 1.0, "control_proxy640_qcli": "jpeg_quality 省略（CLI 既定 0.92）"},
            "full": {"path": "coreimage::develop(DevelopMode::Full)", "max_edge": null,
                "jpeg_quality": "省略（CLI 既定 0.92、Request.swift defaultJPEGQuality）",
                "adjustments": "normalize_adjustments 済み", "exif_inherit": "アプリと同じ（出力は work/ のみ）"},
            "downsample": "面積平均（cv2.INTER_AREA と同じ被覆率の重み）。8bit sRGB 符号化値 /255 のまま、線形化なし。寸法は同じ集合の代理と同一",
            "roi": {"side_frac_of_long_edge": ROI_FRAC, "selection": "natural の 640 代理で center / detail（平均勾配最大）/ flat_dark（輝度下位 25% タイルで勾配最小、白飛び<1%）/ flat_mid（輝度 0.2–0.8 で勾配最小）"},
            "band_matched_sigma": "1.1 × long_edge / 640",
            "sweeps": {"sharpness": SWEEP_SHARPNESS, "noise_reduction": SWEEP_NR, "enabled": sweeps},
            "direction_eps": DIRECTION_EPS,
        },
        "pending_cases": pending,
    });
    write_json(&out.join("provenance.json"), &provenance);

    let mut measurements = Vec::new();
    let mut case_errors = Vec::new();
    for (i, (root_key, root, capture, preset)) in cases.iter().enumerate() {
        let started = Instant::now();
        let input = CaseInput {
            root_key,
            root,
            capture,
            preset,
        };
        match run_case(
            &input,
            &sidecar,
            &out,
            budget,
            seed_override,
            extra_edge,
            sweeps,
        ) {
            Ok((record, measurement)) => {
                let key = record["key"].as_str().unwrap().to_string();
                write_json(&out.join("cases").join(format!("{key}.json")), &record);
                write_json(
                    &out.join("measurements").join(format!("{key}.json")),
                    &measurement,
                );
                println!(
                    "[{}/{}] {key}: {:.1}s sets={} errors={}",
                    i + 1,
                    cases.len(),
                    started.elapsed().as_secs_f64(),
                    measurement["sets"].as_array().map_or(0, Vec::len),
                    measurement["errors"].as_array().map_or(0, Vec::len),
                );
                measurements.push(measurement);
            }
            Err(e) => {
                println!("[{}/{}] {capture}:{preset} 失敗: {e}", i + 1, cases.len());
                case_errors
                    .push(json!({"case": format!("{root_key}__{capture}__{preset}"), "error": e}));
            }
        }
    }
    if let Err(e) = fs::remove_dir_all(out.join("scratch")) {
        println!("警告: scratch を消せません: {e}");
    }
    provenance["case_errors"] = json!(case_errors);
    provenance["cases_measured"] = json!(measurements.len());
    write_json(&out.join("provenance.json"), &provenance);
    let summary = summarize(&measurements);
    write_json(&out.join("proxy_full_summary.json"), &summary);
    fs::write(
        out.join("proxy_full_summary.md"),
        render_markdown(&summary, &provenance),
    )
    .unwrap();
    println!("出力: {}", out.display());
    assert!(
        case_errors.is_empty(),
        "失敗したケースがあります: {case_errors:?}"
    );
    assert_eq!(measurements.len(), cases.len());
    assert_eq!(
        summary["render_errors"],
        json!(0),
        "描画・指標・セッション終了のエラーがあります（measurements/*.json の errors）"
    );
}

#[cfg(test)]
mod unit {
    use super::*;

    #[test]
    fn area_resize_averages_exact_coverage() {
        // 3 → 2 は被覆率 [1, 0.5] / [0.5, 1] の重み。
        let src = image::RgbImage::from_fn(3, 1, |x, _| image::Rgb([[0, 255, 51][x as usize]; 3]));
        let out = area_resize(&src, 2, 1).unwrap();
        assert!((out.pixels[0][0] - (0.0 + 0.5) / 1.5).abs() < 1e-6);
        assert!((out.pixels[1][0] - (0.5 + 0.2) / 1.5).abs() < 1e-6);
        let flat = image::RgbImage::from_pixel(6000, 4, image::Rgb([255, 255, 255]));
        let out = area_resize(&flat, 640, 1).unwrap();
        assert!(out.pixels.iter().flatten().all(|v| *v == 1.0));
    }

    #[test]
    fn rois_are_deterministic_and_inside_frame() {
        let pixels = (0..640 * 427)
            .map(|i| {
                let (x, y) = (i % 640, i / 640);
                let v = if x > 400 {
                    ((x + y) % 7) as f32 / 7.0
                } else {
                    y as f32 / 427.0 * 0.5
                };
                [v; 3]
            })
            .collect();
        let img = RgbImage::new(640, 427, pixels).unwrap();
        let rois = select_rois(&img);
        assert_eq!(rois, select_rois(&img));
        assert!(rois
            .iter()
            .any(|(n, f)| n == "detail" && f[0] >= 400.0 / 640.0 - ROI_FRAC));
        for (_, f) in &rois {
            let (x, y, w, h) = rect(*f, 6000, 4000);
            assert!(x + w <= 6000 && y + h <= 4000 && w >= 700);
            assert!(crop(&img, *f).is_ok());
        }
    }

    #[test]
    fn rank_statistics_handle_ties_and_constants() {
        assert_eq!(ranks(&[3.0, 1.0, 3.0]), vec![2.5, 1.0, 2.5]);
        assert!((spearman(&[1.0, 2.0, 3.0], &[10.0, 20.0, 30.0]).unwrap() - 1.0).abs() < 1e-12);
        assert!(pearson(&[1.0, 1.0, 1.0], &[1.0, 2.0, 3.0]).is_none());
        let s = pair_stats(&[0.01, -0.01, 0.0], &[0.02, 0.01, 0.01]);
        assert_eq!(s["direction_disagree"], json!(1));
        assert_eq!(s["one_sided_change"], json!(1));
    }
}
