use super::{
    image_ops::{gaussian, std},
    native_diagnostics, pso,
    renderer::{error, Evaluator, Renderer, ScratchParent, MAX_EDGE},
    request_identity::RequestIdentity,
    selection::{guard_failure, reasons, score},
    stars::{detect_star_scene, ExifHints},
    RgbImage,
};
use crate::{
    coreimage::{self, CoreImageError},
    presets::resolve_adjustments,
};
use serde_json::{json, Value};
use std::{path::Path, time::Instant};
use tauri::{AppHandle, Emitter, Manager};

const STYLES: [(&str, &str, &str); 5] = [
    (
        "natural",
        "自然",
        "プリセット値のままの基準候補（他の候補との比較の基準）",
    ),
    (
        "stars",
        "星空",
        "星を残しながら暗い背景の階調と星色を守る候補",
    ),
    (
        "foreground",
        "前景を明るく",
        "暗い前景を持ち上げ、空や明部を過度に壊さない候補",
    ),
    (
        "low_noise",
        "低ノイズ",
        "平坦な暗部のノイズを抑え、細部を残す候補",
    ),
    (
        "social",
        "SNS向け",
        "小さな画面でも見やすいコントラストと色に寄せた候補",
    ),
];
fn style_signals(image: &RgbImage) -> (f64, f64) {
    let gray = image.gray();
    let shadow = gray.iter().filter(|v| **v <= 0.08).count() as f64 / gray.len() as f64;
    let blurred = gaussian(&gray, image.width, image.height, 1.0);
    let noise = std(&gray
        .iter()
        .zip(blurred)
        .map(|(a, b)| a - b)
        .collect::<Vec<_>>());
    (shadow, noise)
}
fn styles(image: &RgbImage, exif: &ExifHints, star: bool) -> Vec<&'static str> {
    let (shadow, noise) = style_signals(image);
    let mut styles = vec!["natural"];
    if star {
        styles.push("stars");
    }
    if shadow >= 0.16 {
        styles.push("foreground");
    }
    if exif.iso.unwrap_or(0.0) >= 1600.0 || noise >= 0.045 || shadow >= 0.35 {
        styles.push("low_noise");
    }
    styles.push("social");
    styles
}
fn request(
    capture: &str,
    preset: &str,
    seed: Option<u32>,
    budget: Option<usize>,
) -> Result<(Value, u32, usize), CoreImageError> {
    if !coreimage::is_valid_capture_id(capture) {
        return Err(CoreImageError::new(
            "INVALID_CAPTURE_ID",
            "無効な撮影IDです",
        ));
    }
    let base = resolve_adjustments(preset, None)?;
    let budget = budget.unwrap_or(48);
    if !(32..=512).contains(&budget) {
        return Err(CoreImageError::new(
            "INVALID_BUDGET",
            "budget は32〜512の整数で指定してください",
        ));
    }
    Ok((
        base,
        seed.unwrap_or_else(|| pso::default_seed(capture, preset)),
        budget,
    ))
}
fn candidate(
    style: &str,
    a: Value,
    m: &super::metrics::Metrics,
    base: &super::metrics::Metrics,
    star: bool,
) -> Value {
    let (_, label, description) = STYLES.iter().find(|s| s.0 == style).unwrap();
    json!({"id":style,"label":label,"description":description,"adjustments":a,"metrics":m,"score":score(m,style,base),"reasons":reasons(style,m,base,star)})
}
/// GPS は読み出さず、星判定に必要な3項目だけを既存 EXIF リーダーから受け取る。
pub(super) fn exif_hints(root: &Path, capture: &str) -> ExifHints {
    let path = std::fs::read_dir(root)
        .ok()
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .find(|p| {
            p.file_stem()
                .is_some_and(|s| s.to_string_lossy() == capture)
                && p.extension().is_some_and(|s| s.eq_ignore_ascii_case("jpg"))
        });
    let Some(path) = path.filter(|p| p.canonicalize().is_ok_and(|p| p.parent() == Some(root)))
    else {
        return ExifHints::default();
    };
    let photo = crate::analysis::read_exif(&path, capture, 0, 0);
    ExifHints {
        exposure_time: photo.shutter,
        f_number: photo.f_number,
        iso: photo.iso.map(|v| v as f64),
    }
}
/// 手動計測テスト専用の既定OFFラッパー。アプリと共通の `generate_in_slot` を、
/// テスト用の独立した実行枠で呼ぶ。
#[cfg(test)]
pub(super) fn generate(
    root: &Path,
    sidecar: &Path,
    scratch: ScratchParent,
    capture: &str,
    preset: &str,
    seed: Option<u32>,
    budget: Option<usize>,
    mut progress: impl FnMut(&str, usize, usize),
) -> Result<Value, CoreImageError> {
    let input = coreimage::resolve_arw(root, capture)?;
    let slot = coreimage::DevelopSlot::new(0);
    let guard = slot.acquire()?;
    generate_in_slot(
        root,
        sidecar,
        &input,
        scratch,
        capture,
        preset,
        seed,
        budget,
        false,
        &guard,
        || Ok(()),
        &mut progress,
    )
}

fn generate_in_slot(
    root: &Path,
    sidecar: &Path,
    input: &Path,
    scratch: ScratchParent,
    capture: &str,
    preset: &str,
    seed: Option<u32>,
    budget: Option<usize>,
    native_roi: bool,
    slot: &coreimage::SlotGuard<'_>,
    mut ensure_current: impl FnMut() -> Result<(), CoreImageError>,
    mut progress: impl FnMut(&str, usize, usize),
) -> Result<Value, CoreImageError> {
    let started = Instant::now();
    let (base, seed, budget) = request(capture, preset, seed, budget)?;
    ensure_current()?;
    let exif = exif_hints(root, capture);
    progress("decode", 0, 1);
    let renderer = Renderer::new_with_scratch_parent(sidecar, input, scratch.clone())?;
    let mut evaluator = Evaluator::new(renderer, &base, exif)?;
    let star = detect_star_scene(&evaluator.image, &exif);
    evaluator.enable_stars(&base, star.is_star_scene)?;
    let base_metrics = evaluator.base_metrics.clone();
    let styles = styles(&evaluator.image, &exif, star.is_star_scene);
    let per_style = (budget / (styles.len() - 1)).max(4);
    let mut candidates = vec![candidate(
        "natural",
        base.clone(),
        &base_metrics,
        &base_metrics,
        star.is_star_scene,
    )];
    let mut rejected = vec![];
    let mut used = vec![pso::vector(&base)];
    let mut evaluations = 0;
    let mut progress_evaluated = 0;
    let estimated = (styles.len() - 1) * per_style;
    for style in &styles[1..] {
        let mut chosen = pso::optimise(&base, &base_metrics, style, per_style, seed, |a, gate| {
            ensure_current()?;
            let evaluated = evaluator.evaluate(a, gate);
            ensure_current()?;
            let m = evaluated?;
            evaluations += 1;
            progress_evaluated += 1;
            progress(style, progress_evaluated, estimated);
            Ok(m)
        })?;
        if chosen.failure.is_none()
            && used
                .iter()
                .any(|v| pso::distance(*v, pso::vector(&chosen.adjustments)) < 0.035)
        {
            let v = pso::vector(&chosen.adjustments);
            let target = pso::target(pso::vector(&base), style);
            let moved = pso::adjustments(
                std::array::from_fn(|i| v[i] * 0.55 + target[i] * 0.45),
                &base,
                pso::noise_reduction(style),
            );
            if pso::safe(pso::vector(&moved)) {
                ensure_current()?;
                let evaluated = evaluator.evaluate(&moved, *style == "stars");
                ensure_current()?;
                let metrics = evaluated?;
                if guard_failure(style, &metrics, &base_metrics).is_none()
                    && (*style != "stars" || metrics.star_count >= base_metrics.star_count)
                {
                    chosen.adjustments = moved;
                    chosen.metrics = metrics;
                }
            }
        }
        if let Some(reason) = chosen.failure {
            rejected.push(json!({"id":style,"reason":reason}));
        } else {
            used.push(pso::vector(&chosen.adjustments));
            candidates.push(candidate(
                style,
                chosen.adjustments,
                &chosen.metrics,
                &base_metrics,
                star.is_star_scene,
            ));
        }
    }
    let proxy = json!({"max_edge":evaluator.image.width.max(evaluator.image.height),"width":evaluator.image.width,"height":evaluator.image.height,
        "source":"coreimage_proxy","decode_edge":MAX_EDGE,"decodes":evaluator.renderer.decodes,"renders":evaluator.renderer.renders,
        "evaluations":evaluations});
    let decode_ms = evaluator.renderer.decode_ms;
    let render_ms = evaluator.renderer.render_ms;
    evaluator.renderer.finish()?;
    ensure_current()?;
    let native_roi_diagnostics = if native_roi {
        match native_diagnostics::diagnose(
            &evaluator.image,
            &candidates,
            sidecar,
            input,
            &scratch,
            slot,
            &mut ensure_current,
            &mut progress,
        ) {
            Ok(d) => d,
            Err(e) if e.code == "CANDIDATES_STALE" => return Err(e),
            Err(e) => native_diagnostics::failed(&e.code),
        }
    } else {
        native_diagnostics::not_run()
    };
    ensure_current()?;
    let elapsed_ms = json!({"total":started.elapsed().as_secs_f64()*1000.0,"decode":decode_ms,"render":render_ms});
    progress("done", estimated, estimated);
    Ok(
        json!({"capture_id":capture,"preset":preset,"seed":seed,"budget":budget,"proxy":proxy,"star_scene":star,"candidates":candidates,"rejected":rejected,"elapsed_ms":elapsed_ms,"native_roi_diagnostics":native_roi_diagnostics}),
    )
}

#[tauri::command]
pub async fn generate_development_candidates(
    app: AppHandle,
    capture_id: String,
    preset: String,
    seed: Option<u32>,
    budget: Option<usize>,
    native_roi_diagnostics: Option<bool>,
) -> Result<Value, CoreImageError> {
    request(&capture_id, &preset, seed, budget)?;
    let data = app
        .path()
        .app_data_dir()
        .map_err(|e| error(e.to_string()))?;
    let sidecar = coreimage::sidecar_path()?;
    // Resolve before queueing. A later folder change must never redirect this request.
    let identity = RequestIdentity::start(&data, &sidecar, &capture_id)?;
    tauri::async_runtime::spawn_blocking(move || {
        let slot = coreimage::DEVELOP_SLOT.acquire()?;
        identity.ensure_current()?;
        let scratch = ScratchParent::for_app_data(&data)?;
        generate_in_slot(&identity.root, &identity.sidecar, &identity.input, scratch,
            &capture_id, &preset, seed, budget, native_roi_diagnostics.unwrap_or(false), &slot,
            || identity.ensure_current(),
            |stage,completed,total| {
                if identity.ensure_current().is_ok() {
                    let _=app.emit("development-candidates-progress",json!({"capture_id":capture_id,"preset":preset,"stage":stage,"completed":completed,"total":total}));
                }
            })
    }).await.map_err(|e|error(format!("候補生成スレッドが異常終了しました: {e}")))?
}

#[cfg(test)]
#[path = "loop_tests.rs"]
mod tests;
