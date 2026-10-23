use super::*;
use crate::candidates::{metrics::Metrics, pso};
use std::fs;

fn scratch_test_root(name: &str) -> std::path::PathBuf {
    let root =
        std::env::temp_dir().join(format!("geoframe-candidate-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).unwrap();
    root
}

#[test]
fn app_scratch_parent_rejects_symlinked_components_and_files() {
    let root = scratch_test_root("path-guard");
    let outside = root.join("outside");
    fs::create_dir(&outside).unwrap();

    let app_data = root.join("linked-candidates");
    fs::create_dir(&app_data).unwrap();
    fs::create_dir(app_data.join("coreimage")).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&outside, app_data.join("coreimage/candidates")).unwrap();
    #[cfg(windows)]
    std::os::windows::fs::symlink_dir(&outside, app_data.join("coreimage/candidates")).unwrap();
    assert!(crate::coreimage::prepare_candidate_scratch_dir(&app_data).is_err());
    assert!(fs::read_dir(&outside).unwrap().next().is_none());

    let app_data = root.join("linked-coreimage");
    fs::create_dir(&app_data).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&outside, app_data.join("coreimage")).unwrap();
    #[cfg(windows)]
    std::os::windows::fs::symlink_dir(&outside, app_data.join("coreimage")).unwrap();
    assert!(crate::coreimage::prepare_candidate_scratch_dir(&app_data).is_err());
    assert!(fs::read_dir(&outside).unwrap().next().is_none());

    let app_data = root.join("file-component");
    fs::create_dir(&app_data).unwrap();
    fs::write(app_data.join("coreimage"), "file").unwrap();
    assert!(crate::coreimage::prepare_candidate_scratch_dir(&app_data).is_err());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn app_scratch_parent_accepts_real_directories() {
    let root = scratch_test_root("path-valid");
    let app_data = root.join("app-data");
    fs::create_dir(&app_data).unwrap();
    let scratch = crate::coreimage::prepare_candidate_scratch_dir(&app_data).unwrap();
    assert_eq!(
        scratch,
        app_data
            .canonicalize()
            .unwrap()
            .join("coreimage/candidates")
    );
    assert!(fs::symlink_metadata(&scratch).unwrap().file_type().is_dir());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn request_validation_and_seed_contract() {
    assert_eq!(
        request("../DSC07402", "nightscape", None, None)
            .unwrap_err()
            .code,
        "INVALID_CAPTURE_ID"
    );
    assert!(request("DSC07402", "unknown", None, None).is_err());
    for b in [0, 31, 513] {
        assert_eq!(
            request("DSC07402", "nightscape", None, Some(b))
                .unwrap_err()
                .code,
            "INVALID_BUDGET"
        );
    }
    let series: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/baseline/candidate_series.json"
    ))
    .unwrap();
    assert_eq!(
        pso::default_seed("DSC07402", "nightscape"),
        series["seed"].as_u64().unwrap() as u32
    );
    assert_eq!(
        request("DSC07402", "nightscape", Some(0), Some(32))
            .unwrap()
            .1,
        0
    );
}

#[test]
fn deterministic_loop_counts_and_decode_keys() {
    let base = resolve_adjustments("nightscape", None).unwrap();
    let run = |seed| {
        let mut sequence = Vec::new();
        let chosen = pso::optimise(&base, &Metrics::default(), "social", 12, seed, |a, _| {
            sequence.push(a.clone());
            Ok(Metrics {
                detail: a["sharpness"].as_f64().unwrap() / 2.0,
                tonal_range: 0.7,
                ..Metrics::default()
            })
        })
        .unwrap();
        (sequence, chosen.adjustments)
    };
    let first = run(42);
    assert_eq!(first, run(42));
    assert_ne!(first.0, run(43).0);
    assert_eq!(first.0.len(), 12);
    for a in &first.0 {
        assert_eq!(a["highlight_recovery"], base["highlight_recovery"]);
        assert_eq!(a["noise_reduction"], "light");
        assert!(pso::exposure_grid(0.0).contains(&a["exposure"].as_f64().unwrap()));
        assert!(a["sharpness"].as_f64().unwrap() >= 1.0);
    }
    let soft = resolve_adjustments("portrait_soft", None).unwrap();
    let a = pso::adjustments([0.0, 20.0, 1.0, 1.0, 1.0, 1.6], &soft, "light");
    assert_eq!(a["sharpness"], soft["sharpness"]);
}

#[test]
fn fixed_series_replays_metric_objectives_without_numpy_rng() {
    let series: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/baseline/candidate_series.json"
    ))
    .unwrap();
    // 6次元化で距離項だけが変わる。#107 の系列から残りの目的関数の数値契約を検証する。
    for input in series["objective_inputs"]
        .as_array()
        .expect("frozen objective inputs")
    {
        let m: Metrics = serde_json::from_value(input["metrics"].clone()).unwrap();
        let style = input["style"].as_str().unwrap();
        let take = |key: &str| std::array::from_fn(|i| input[key][i].as_f64().unwrap());
        let actual = pso::objectives(
            &m,
            style,
            take("vector"),
            take("target"),
            take("base_vector"),
            input["star_reference"].as_u64().unwrap() as usize,
            input["guard_penalty"].as_f64().unwrap(),
        );
        for i in [0, 1, 2, 3, 4, 5, 6, 7, 10, 11] {
            assert!(
                (actual[i]
                    - input["objectives_before_safety_penalty"][i]
                        .as_f64()
                        .unwrap())
                .abs()
                    < 1e-10
            );
        }
    }
}

#[test]
fn final_selection_does_not_lose_stars_or_bypass_guards() {
    let base = resolve_adjustments("nightscape", None).unwrap();
    let baseline = Metrics {
        star_count: 100,
        ..Metrics::default()
    };
    let result = pso::optimise(&base, &baseline, "stars", 12, 7, |a, _| {
        Ok(Metrics {
            star_count: if a["exposure"].as_f64().unwrap() < 0.0 {
                50
            } else {
                110
            },
            ..Metrics::default()
        })
    })
    .unwrap();
    assert!(result.failure.is_none());
    assert!(result.metrics.star_count >= 100);
    let rejected = pso::optimise(&base, &baseline, "foreground", 12, 7, |_, _| {
        Ok(Metrics::default())
    })
    .unwrap();
    assert!(rejected.failure.is_some());
    let rejected = pso::optimise(&base, &baseline, "stars", 12, 7, |_, _| {
        Ok(Metrics {
            star_count: 20,
            ..Metrics::default()
        })
    })
    .unwrap();
    assert!(rejected.failure.is_some());
}

/// 手動測定（release sidecar 必須）:
/// GEOFRAME_PSO_RAW_ROOT=/read-only/raw cargo test --release --manifest-path src-tauri/Cargo.toml candidates_release_benchmark -- --ignored --nocapture
/// 出力は work/issue87/measurements.json（画像・EXIF・位置情報を保存しない）。
#[test]
#[ignore]
fn candidates_release_benchmark() {
    assert!(!cfg!(debug_assertions), "--release で実行してください");
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let root = std::env::var_os("GEOFRAME_PSO_RAW_ROOT")
        .expect("GEOFRAME_PSO_RAW_ROOT を指定してください");
    let root = Path::new(&root).canonicalize().unwrap();
    let sidecar = std::env::var_os("GEOFRAME_PSO_SIDECAR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            repo.join("src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin")
        });
    let work = repo.join("work/issue87");
    fs::create_dir_all(&work).unwrap();
    let cases: Value = serde_json::from_str(include_str!(
        "../../../scripts/eval_cases/candidates_baseline.json"
    ))
    .unwrap();
    let mut results = Vec::new();
    for case in cases["sets"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["root"] == "stars")
        .unwrap()["cases"]
        .as_array()
        .unwrap()
    {
        let (capture, preset) = case.as_str().unwrap().split_once(':').unwrap();
        let result = generate(
            &root,
            &sidecar,
            ScratchParent::for_directory(&work).unwrap(),
            capture,
            preset,
            None,
            None,
            |_, _, _| {},
        )
        .unwrap();
        println!(
            "{capture}: {:.3}s decodes={} renders={} candidates={}",
            result["elapsed_ms"]["total"].as_f64().unwrap() / 1000.0,
            result["proxy"]["decodes"],
            result["proxy"]["renders"],
            result["candidates"]
                .as_array()
                .unwrap()
                .iter()
                .map(|c| c["id"].as_str().unwrap())
                .collect::<Vec<_>>()
                .join(",")
        );
        results.push(result);
    }
    fs::write(
        work.join("measurements.json"),
        serde_json::to_vec_pretty(&results).unwrap(),
    )
    .unwrap();
}

#[cfg(unix)]
#[test]
fn renderer_reuses_decode_handles_recovers_and_cleans_scratch() {
    use std::os::unix::fs::PermissionsExt;
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let parent = repo.join("work/issue87-tests");
    fs::create_dir_all(&parent).unwrap();
    let dir = parent.join(format!("renderer-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    struct Cleanup(std::path::PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    let _cleanup = Cleanup(dir.clone());
    let image = image::RgbImage::from_fn(8, 8, |x, y| {
        image::Rgb([(x * 30) as u8, (y * 30) as u8, 60])
    });
    image.save(dir.join("source.jpg")).unwrap();
    let program = dir.join("sidecar");
    fs::write(&program,r#"#!/bin/sh
cd "$(dirname "$0")" || exit 8
n=0
while IFS= read -r line; do
 n=$((n+1))
 case "$line" in
  *'"op":"decode"'*) printf '{"ok":true,"handle":"h%d","sequence":%d}\n' "$n" "$n" ;;
  *'"op":"render"'*)
   if [ ! -f recovered ]; then
    touch recovered
    printf '{"ok":false,"error":{"code":"HANDLE_NOT_FOUND","message":"evicted"},"sequence":%d}\n' "$n"
   else
    output=$(printf '%s' "$line" | sed -E 's/.*"output":"([^"]*)".*/\1/')
    command cp -f source.jpg "$output" || exit 7
    printf '{"ok":true,"sequence":%d}\n' "$n"
   fi ;;
  *) printf '{"ok":true,"released":true,"sequence":%d}\n' "$n" ;;
 esac
done
"#).unwrap();
    fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
    let scratch = dir.join("scratch");
    let mut renderer = Renderer::new(&program, &dir.join("input.ARW"), &scratch).unwrap();
    let run_dir = fs::read_dir(&scratch)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    assert_eq!(
        fs::metadata(&run_dir).unwrap().permissions().mode() & 0o777,
        0o700
    );
    let base = resolve_adjustments("nightscape", None).unwrap();
    renderer.render(&base).unwrap();
    assert_eq!(renderer.decodes, 2); // 最初のハンドルが失われても1回だけ再decode。
    let mut changed = base.clone();
    changed["brightness"] = json!(1.1);
    renderer.render(&changed).unwrap();
    assert_eq!(renderer.decodes, 2);
    changed["exposure"] = json!(0.5);
    renderer.render(&changed).unwrap();
    assert_eq!(renderer.decodes, 3);
    changed["noise_reduction"] = json!("light");
    renderer.render(&changed).unwrap();
    assert_eq!(renderer.decodes, 4);
    renderer.finish().unwrap();
    assert_eq!(fs::read_dir(&scratch).unwrap().count(), 0);
    let mut failed = Renderer::new(&program, &dir.join("input.ARW"), &scratch).unwrap();
    fs::remove_file(dir.join("source.jpg")).unwrap();
    assert!(failed.render(&base).is_err());
    drop(failed);
    assert_eq!(fs::read_dir(&scratch).unwrap().count(), 0);
}

#[test]
#[ignore]
fn candidate_threshold_survey() {
    assert!(!cfg!(debug_assertions));
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let root = std::env::var_os("GEOFRAME_PSO_RAW_ROOT")
        .expect("GEOFRAME_PSO_RAW_ROOT を指定してください");
    let root = Path::new(&root).canonicalize().unwrap();
    let sidecar = repo.join("src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin");
    let scratch = repo.join("work/issue87");
    let baseline: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/baseline/candidates.json"
    ))
    .unwrap();
    let mut survey = Vec::new();
    for row in baseline["records"].as_array().unwrap() {
        let capture = row["case"]["capture_id"].as_str().unwrap();
        let input = coreimage::resolve_arw(&root, capture).unwrap();
        let base = resolve_adjustments("nightscape", None).unwrap();
        let mut renderer = Renderer::new(&sidecar, &input, &scratch).unwrap();
        let image = renderer.render(&base).unwrap();
        let (shadow, noise) = style_signals(&image);
        let exif = exif_hints(&root, capture);
        println!(
            "{capture}: style_noise={noise:.6} shadow_ratio={shadow:.6} exposure_time={:?}",
            exif.exposure_time
        );
        survey.push(json!({"capture_id":capture,"style_noise":noise,"shadow_ratio":shadow,"exposure_time":exif.exposure_time}));
        renderer.finish().unwrap();
    }
    fs::write(
        scratch.join("threshold-survey.json"),
        serde_json::to_vec_pretty(&survey).unwrap(),
    )
    .unwrap();
}
