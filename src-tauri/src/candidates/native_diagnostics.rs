//! Opt-in observations only. Nothing in this module influences proxy selection.
use super::{
    image_ops::round6,
    renderer::{Scratch, ScratchParent},
    roi::{crop_u8, measure, rect, select_rois},
    RgbImage,
};
use crate::{
    adjustments::normalize_adjustments,
    coreimage::{self, CoreImageError, DevelopMode, SlotGuard},
};
use serde_json::{json, Value};
use std::{
    path::Path,
    time::{Duration, Instant},
};

pub(super) const MAX_FULL_SETTINGS: usize = 5;
const FULL_TIMEOUT: Duration = Duration::from_secs(180);
const NAMES: [&str; 4] = ["center", "detail", "flat_dark", "flat_mid"];
const METRICS: [&str; 3] = ["detail", "noise_flat", "halo"];

pub(super) fn not_run() -> Value {
    json!({"status":"not_run", "reason":"opt_in_disabled", "rois":[], "sets":[],
        "unique_settings":0,"renders":0,"elapsed_ms":{"total":0.0}})
}
pub(super) fn failed(reason: &str) -> Value {
    json!({"status":"failed","reason":reason,"rois":[],"sets":[],
        "unique_settings":0,"renders":0,"elapsed_ms":{"total":0.0}})
}

/// Normalize only the diagnostic copies; proxy adjustments and candidate IDs stay untouched.
fn unique_settings(candidates: &[Value]) -> Result<Vec<Value>, CoreImageError> {
    let mut sets: Vec<Value> = Vec::new();
    for candidate in candidates {
        let a = normalize_adjustments(&candidate["adjustments"])?;
        if let Some(set) = sets.iter_mut().find(|s| s["adjustments"] == a) {
            set["candidate_ids"]
                .as_array_mut()
                .unwrap()
                .push(candidate["id"].clone());
        } else {
            sets.push(json!({"adjustments":a,"candidate_ids":[candidate["id"]]}));
        }
    }
    if sets.len() > MAX_FULL_SETTINGS
        || candidates.first().and_then(|c| c["id"].as_str()) != Some("natural")
    {
        return Err(CoreImageError::new(
            "NATIVE_ROI_SETTINGS_INVALID",
            "原寸診断の設定数または natural 基準が不正です",
        ));
    }
    Ok(sets)
}

fn deltas(absolute: &Value, natural: &Value, names: &[&str]) -> Value {
    let mut result = serde_json::Map::new();
    for name in names {
        result.insert(
            (*name).into(),
            match (absolute[name].as_f64(), natural[name].as_f64()) {
                (Some(a), Some(b)) => json!(round6(a - b)),
                _ => Value::Null,
            },
        );
    }
    Value::Object(result)
}

pub(super) fn diagnose(
    proxy: &RgbImage,
    candidates: &[Value],
    sidecar: &Path,
    input: &Path,
    scratch_parent: &ScratchParent,
    slot: &SlotGuard<'_>,
    mut ensure_current: impl FnMut() -> Result<(), CoreImageError>,
    mut progress: impl FnMut(&str, usize, usize),
) -> Result<Value, CoreImageError> {
    let started = Instant::now();
    let mut sets = unique_settings(candidates)?;
    let scratch = Scratch::new(scratch_parent).map_err(|_| {
        CoreImageError::new("NATIVE_ROI_SCRATCH_FAILED", "原寸診断のscratchを作れません")
    })?;
    let rois = select_rois(proxy);
    let roi_status: Vec<Value> = NAMES.iter().map(|name| match rois.iter().find(|r| r.0 == *name) {
        Some((_, frac)) => json!({"name":name,"status":"selected","normalized":frac,"reason":null}),
        None => json!({"name":name,"status":"missing","normalized":null,"reason":"no_eligible_natural_proxy_tile"}),
    }).collect();
    let mut baseline: Vec<RgbImage> = Vec::new();
    let mut natural_measurements: Vec<Value> = Vec::new();
    let mut baseline_dimensions = None;
    let mut renders = 0;
    let count = sets.len();
    progress("native_roi", 0, count);
    for (index, set) in sets.iter_mut().enumerate() {
        ensure_current()?;
        let set_started = Instant::now();
        if rois.is_empty() || (index > 0 && baseline.is_empty()) {
            set["status"] = json!("missing");
            set["reason"] = json!(if rois.is_empty() {
                "no_eligible_rois"
            } else {
                "natural_full_unavailable"
            });
            set["rois"] = json!([]);
        } else {
            // The batch proxy process has already exited. One full process and one u8 frame at a time.
            let output = scratch.0.join("native.jpg");
            let render_started = Instant::now();
            renders += 1;
            let rendered = coreimage::develop_to_output_in_slot(
                sidecar,
                input,
                &output,
                DevelopMode::Full,
                Some(&set["adjustments"]),
                FULL_TIMEOUT,
                slot,
            );
            let render_ms = render_started.elapsed().as_secs_f64() * 1000.0;
            let crop_started = Instant::now();
            let cropped = rendered.and_then(|_| {
                let full = image::open(&output)
                    .map_err(|_| {
                        CoreImageError::new(
                            "NATIVE_ROI_IMAGE_READ_FAILED",
                            "原寸診断画像を読めません",
                        )
                    })?
                    .into_rgb8();
                let dims = (full.width() as usize, full.height() as usize);
                if baseline_dimensions.is_some_and(|b| b != dims) {
                    return Err(CoreImageError::new(
                        "NATIVE_ROI_DIMENSIONS_MISMATCH",
                        "原寸診断の natural と候補の寸法が一致しません",
                    ));
                }
                let crops = rois
                    .iter()
                    .map(|(_, frac)| {
                        crop_u8(&full, *frac).map_err(|_| {
                            CoreImageError::new(
                                "NATIVE_ROI_CROP_FAILED",
                                "原寸ROIの切り出しに失敗しました",
                            )
                        })
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                Ok((dims, crops))
            }); // full u8 buffer is dropped here; only ROI float images survive.
            let crop_ms = crop_started.elapsed().as_secs_f64() * 1000.0;
            let _ = std::fs::remove_file(&output);
            match cropped {
                Err(e) => {
                    set["status"] = json!("failed");
                    // Do not echo CLI messages that may contain private paths or metadata.
                    set["reason"] = json!(e.code);
                    set["rois"] = json!([]);
                }
                Ok((dims, crops)) => {
                    if index == 0 {
                        baseline_dimensions = Some(dims);
                    }
                    let metrics_started = Instant::now();
                    let mut observations = Vec::new();
                    let mut measured_natural = Vec::new();
                    for (i, ((name, frac), img)) in rois.iter().zip(&crops).enumerate() {
                        let base = if index == 0 { img } else { &baseline[i] };
                        let observation = match measure(base, img, dims.0.max(dims.1)) {
                            Ok(m) => {
                                let natural = if index == 0 {
                                    &m
                                } else {
                                    &natural_measurements[i]
                                };
                                let absolute = json!({"detail":m["metrics"]["detail"],"noise_flat":m["metrics"]["noise_flat"],"halo":m["metrics"]["halo"]});
                                let observation = json!({"name":name,"status":"complete","reason":null,"normalized":frac,
                                    "pixels":rect(*frac,dims.0,dims.1),"absolute":absolute,
                                    "delta_vs_natural":deltas(&m["metrics"],&natural["metrics"],&METRICS),
                                    "bands":m["diag"],"bands_delta_vs_natural":deltas(&m["diag"],&natural["diag"],&["band_px","band_matched"])});
                                if index == 0 {
                                    measured_natural.push(m);
                                }
                                observation
                            }
                            Err(_) => {
                                json!({"name":name,"status":"failed","reason":"roi_measurement_failed","normalized":frac})
                            }
                        };
                        observations.push(observation);
                    }
                    if index == 0 && measured_natural.len() == rois.len() {
                        natural_measurements = measured_natural;
                        baseline = crops;
                    }
                    set["status"] =
                        json!(if observations.iter().any(|o| o["status"] == "failed") {
                            "failed"
                        } else if rois.len() < NAMES.len() {
                            "missing"
                        } else {
                            "complete"
                        });
                    set["reason"] = if set["status"] == "complete" {
                        Value::Null
                    } else {
                        json!("roi_measurement_incomplete")
                    };
                    set["dimensions"] = json!([dims.0, dims.1]);
                    set["rois"] = json!(observations);
                    set["metrics_ms"] = json!(metrics_started.elapsed().as_secs_f64() * 1000.0);
                }
            }
            set["render_ms"] = json!(render_ms);
            set["crop_ms"] = json!(crop_ms);
        }
        set["total_ms"] = json!(set_started.elapsed().as_secs_f64() * 1000.0);
        progress("native_roi", index + 1, count);
    }
    ensure_current()?;
    let status = if sets.iter().any(|s| s["status"] == "failed") {
        "failed"
    } else if rois.len() < NAMES.len() || sets.iter().any(|s| s["status"] == "missing") {
        "missing"
    } else {
        "complete"
    };
    Ok(
        json!({"status":status,"reason":if status == "complete" {Value::Null} else {json!("native_roi_incomplete")},
        "rois":roi_status,"sets":sets,"unique_settings":count,"renders":renders,
        "elapsed_ms":{"total":started.elapsed().as_secs_f64()*1000.0}}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{coreimage::DevelopSlot, presets::resolve_adjustments};
    use std::{
        fs,
        sync::atomic::{AtomicUsize, Ordering},
    };
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    struct Fixture(std::path::PathBuf);
    impl Fixture {
        fn new(script: &str) -> Self {
            use std::os::unix::fs::PermissionsExt;
            let path = std::env::temp_dir().join(format!(
                "geoframe-native-roi-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            let program = path.join("cli");
            fs::write(&program, script).unwrap();
            fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
            let img =
                image::RgbImage::from_fn(64, 48, |x, y| image::Rgb([((x + y) * 2 + 16) as u8; 3]));
            img.save(path.join("source.jpg")).unwrap();
            fs::write(path.join("input.ARW"), "mock").unwrap();
            Self(path)
        }
        fn proxy(&self) -> RgbImage {
            RgbImage::from_dynamic(&image::open(self.0.join("source.jpg")).unwrap()).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    const COPY: &str = r#"#!/bin/sh
IFS= read -r line
output=$(printf '%s' "$line" | sed -E 's/.*"output":"([^"]*)".*/\1/')
base=$(dirname "$0")
printf '%s\n' "$line" >> "$base/requests"
cp "$base/source.jpg" "$output" || exit 7
printf '{"ok":true,"output":"%s","width":64,"height":48}\n' "$output"
"#;
    fn candidates() -> Vec<Value> {
        let base = resolve_adjustments("nightscape", None).unwrap();
        ["natural", "stars", "foreground", "low_noise", "social"]
            .iter()
            .enumerate()
            .map(|(i, id)| {
                let mut a = base.clone();
                a["sharpness"] = json!(1.0 + i as f64 * 0.1);
                json!({"id":id,"adjustments":a})
            })
            .collect()
    }
    #[test]
    fn normalized_settings_share_calls_and_enforce_five_setting_ceiling() {
        let mut c = candidates();
        c[1]["adjustments"] = c[0]["adjustments"].clone();
        let sets = unique_settings(&c).unwrap();
        assert_eq!(sets.len(), 4);
        assert_eq!(sets[0]["candidate_ids"], json!(["natural", "stars"]));
        c[0]["adjustments"]["sharpness"] = json!(1.8);
        c[1]["adjustments"]["sharpness"] = json!(1.6);
        assert_eq!(unique_settings(&c).unwrap().len(), 4);
        let mut c = candidates();
        let mut extra = c[0].clone();
        extra["id"] = json!("extra");
        extra["adjustments"]["sharpness"] = json!(1.55);
        c.push(extra);
        assert!(unique_settings(&c).is_err());
    }
    #[test]
    fn sequential_full_calls_keep_slot_and_clean_only_owned_scratch() {
        let fixture = Fixture::new(COPY);
        let scratch = ScratchParent::for_directory(&fixture.0.join("scratch")).unwrap();
        fs::write(fixture.0.join("accepted.jpg"), "user-output").unwrap();
        let slot = DevelopSlot::new(0);
        let guard = slot.acquire().unwrap();
        let c = candidates();
        let original = c.clone();
        let mut stages = Vec::new();
        let result = diagnose(
            &fixture.proxy(),
            &c,
            &fixture.0.join("cli"),
            &fixture.0.join("input.ARW"),
            &scratch,
            &guard,
            || Ok(()),
            |stage, done, total| stages.push((stage.to_string(), done, total)),
        )
        .unwrap();
        assert_eq!(c, original);
        assert_eq!(result["renders"], 5);
        assert_eq!(result["status"], "complete");
        assert_eq!(stages.last().unwrap(), &("native_roi".into(), 5, 5));
        assert_eq!(fs::read_dir(fixture.0.join("scratch")).unwrap().count(), 0);
        assert_eq!(
            fs::read_to_string(fixture.0.join("accepted.jpg")).unwrap(),
            "user-output"
        );
        let requests = fs::read_to_string(fixture.0.join("requests")).unwrap();
        assert_eq!(requests.lines().count(), 5);
        for line in requests.lines() {
            let r: Value = serde_json::from_str(line).unwrap();
            assert_eq!(r["mode"], "full");
            assert!(r.get("max_edge").is_none());
            assert!(r.get("jpeg_quality").is_none());
            assert_eq!(r["settings"]["profile"], "apple_default");
            assert_eq!(r["settings"]["lens_correction"], true);
        }
        for roi in result["sets"][0]["rois"].as_array().unwrap() {
            assert_eq!(
                roi["delta_vs_natural"],
                json!({"detail":0.0,"noise_flat":0.0,"halo":0.0})
            );
        }
        assert_eq!(slot.acquire().err().unwrap().code, "BUSY");
        drop(guard);
        assert!(slot.acquire().is_ok());
    }
    #[test]
    fn duplicate_settings_render_once_and_missing_roi_stays_missing() {
        let fixture = Fixture::new(COPY);
        let scratch = ScratchParent::for_directory(&fixture.0.join("scratch")).unwrap();
        let slot = DevelopSlot::new(0);
        let guard = slot.acquire().unwrap();
        let mut c = candidates();
        for i in 1..c.len() {
            c[i]["adjustments"] = c[0]["adjustments"].clone();
        }
        let proxy = RgbImage::new(64, 48, vec![[0.01; 3]; 64 * 48]).unwrap();
        let result = diagnose(
            &proxy,
            &c,
            &fixture.0.join("cli"),
            &fixture.0.join("input.ARW"),
            &scratch,
            &guard,
            || Ok(()),
            |_, _, _| {},
        )
        .unwrap();
        assert_eq!(result["renders"], 1);
        assert_eq!(result["status"], "missing");
        assert!(result["rois"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["name"] == "flat_mid" && r["status"] == "missing"));
        assert_eq!(
            result["sets"][0]["candidate_ids"].as_array().unwrap().len(),
            5
        );
    }
    #[test]
    fn cli_failure_is_not_success_and_baseline_failure_does_not_invent_deltas() {
        let fixture=Fixture::new("#!/bin/sh\nprintf '{\"ok\":false,\"error\":{\"code\":\"DECODE_FAILED\",\"message\":\"private\"}}\\n'\nexit 1\n");
        let scratch = ScratchParent::for_directory(&fixture.0.join("scratch")).unwrap();
        let slot = DevelopSlot::new(0);
        let guard = slot.acquire().unwrap();
        let result = diagnose(
            &fixture.proxy(),
            &candidates(),
            &fixture.0.join("cli"),
            &fixture.0.join("input.ARW"),
            &scratch,
            &guard,
            || Ok(()),
            |_, _, _| {},
        )
        .unwrap();
        assert_eq!(result["status"], "failed");
        assert_eq!(result["renders"], 1);
        assert_eq!(result["sets"][0]["reason"], "DECODE_FAILED");
        assert_eq!(result["sets"][1]["reason"], "natural_full_unavailable");
        assert!(!result.to_string().contains("private"));
        assert_eq!(fs::read_dir(fixture.0.join("scratch")).unwrap().count(), 0);
    }
    #[test]
    fn common_cli_watchdog_times_out_with_existing_slot_and_releases_after_error() {
        let fixture = Fixture::new("#!/bin/sh\nexec sleep 5\n");
        let slot = DevelopSlot::new(0);
        let guard = slot.acquire().unwrap();
        let err = coreimage::develop_to_output_in_slot(
            &fixture.0.join("cli"),
            &fixture.0.join("input.ARW"),
            &fixture.0.join("timeout.jpg"),
            DevelopMode::Full,
            Some(&candidates()[0]["adjustments"]),
            Duration::from_millis(30),
            &guard,
        )
        .unwrap_err();
        assert_eq!(err.code, "SIDECAR_TIMEOUT");
        drop(guard);
        assert!(slot.acquire().is_ok());
    }
    #[test]
    fn stale_during_full_discards_result_and_cleans_scratch() {
        let fixture = Fixture::new(COPY);
        let scratch = ScratchParent::for_directory(&fixture.0.join("scratch")).unwrap();
        let slot = DevelopSlot::new(0);
        let guard = slot.acquire().unwrap();
        let mut checks = 0;
        let err = diagnose(
            &fixture.proxy(),
            &candidates(),
            &fixture.0.join("cli"),
            &fixture.0.join("input.ARW"),
            &scratch,
            &guard,
            || {
                checks += 1;
                if checks > 1 {
                    Err(CoreImageError::new("CANDIDATES_STALE", "stale"))
                } else {
                    Ok(())
                }
            },
            |_, _, _| {},
        )
        .unwrap_err();
        assert_eq!(err.code, "CANDIDATES_STALE");
        assert_eq!(fs::read_dir(fixture.0.join("scratch")).unwrap().count(), 0);
    }
}
