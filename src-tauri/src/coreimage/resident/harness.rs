//! Ignored real-CLI comparison through the shared Rust develop path (not app E2E).
use super::super::transport_bench::{compare_rgb, standard_series, stats};
use super::*;

struct Config {
    sidecar: PathBuf,
    roots: [PathBuf; 2],
    captures: [String; 2],
    out: PathBuf,
    rounds: usize,
    timeout: Duration,
}

fn rss_kib(pid: u32) -> Option<u64> {
    let out = Command::new("ps")
        .args(["-o", "rss=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    String::from_utf8_lossy(&out.stdout).trim().parse().ok()
}

fn io_error(_: std::io::Error) -> CoreImageError {
    CoreImageError::new("HARNESS_IO", "harness scratch I/O failed")
}

/// New exclusive directory, with canonical parent checked before any write. Never reuse an
/// existing OUT, follow an OUT symlink, or put scratch inside/above a source directory.
fn prepare(cfg: &Config) -> Result<PathBuf, CoreImageError> {
    let parent = cfg
        .out
        .parent()
        .ok_or_else(|| CoreImageError::new("HARNESS_OUTPUT", "OUT needs a parent"))?
        .canonicalize()
        .map_err(io_error)?;
    let name = cfg
        .out
        .file_name()
        .ok_or_else(|| CoreImageError::new("HARNESS_OUTPUT", "OUT needs a new directory name"))?;
    let out = parent.join(name);
    for root in &cfg.roots {
        let root = root.canonicalize().map_err(io_error)?;
        if out.starts_with(&root) || root.starts_with(&out) {
            return Err(CoreImageError::new(
                "HARNESS_OUTPUT",
                "OUT and source must be disjoint",
            ));
        }
    }
    fs::create_dir(&out).map_err(io_error)?;
    Ok(out)
}

fn run(cfg: &Config, binary: &'static str) -> Result<Value, CoreImageError> {
    let roots = [
        cfg.roots[0].canonicalize().map_err(io_error)?,
        cfg.roots[1].canonicalize().map_err(io_error)?,
    ];
    // Resolve all inputs before writing scratch; never enumerate photos beyond explicit IDs.
    for root in &roots {
        for id in &cfg.captures {
            resolve_arw(root, id)?;
        }
    }
    let out = prepare(cfg)?;
    let app = out.join("app_data");
    fs::create_dir(&app).map_err(io_error)?;
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cfg.sidecar, Policy::default(), &slot)?;
    let mut records = vec![];
    let mut boundary_rss = vec![];
    let mut serial = 0;
    let mut previous_pid = None;
    for series in standard_series() {
        for (step, adjustments) in series.steps.iter().enumerate() {
            // A/RAW0, A/RAW1, B/RAW0, B/RAW1. One resident spans every folder and series.
            for round in 0..cfg.rounds {
                let folder = (round / 2) % 2;
                let capture = round % 2;
                fs::write(
                    app.join("config.json"),
                    json!({"source_root": roots[folder]}).to_string(),
                )
                .map_err(io_error)?;
                let preview = app.join("coreimage/preview");
                if preview.exists() {
                    fs::remove_dir_all(&preview).map_err(io_error)?;
                }
                let started = Instant::now();
                let single = develop(
                    &app,
                    &cfg.sidecar,
                    &cfg.captures[capture],
                    DevelopMode::Preview,
                    Some(adjustments),
                    &slot,
                    cfg.timeout,
                )?;
                let single_ms = started.elapsed().as_secs_f64() * 1000.0;
                assert!(!single.cache_hit);
                let single_prov: Value = serde_json::from_str(
                    &fs::read_to_string(&single.provenance_path).map_err(io_error)?,
                )
                .map_err(|_| CoreImageError::new("HARNESS_JSON", "invalid private provenance"))?;
                let reference = out.join(format!("reference-{serial}.jpg"));
                fs::copy(&single.output_path, &reference).map_err(io_error)?;
                fs::remove_file(&single.output_path).map_err(io_error)?;
                fs::remove_file(&single.provenance_path).map_err(io_error)?;
                let started = Instant::now();
                let resident = develop_with_executor(
                    &app,
                    &cfg.sidecar,
                    &cfg.captures[capture],
                    DevelopMode::Preview,
                    Some(adjustments),
                    &slot,
                    cfg.timeout,
                    &owner,
                )?;
                let resident_ms = started.elapsed().as_secs_f64() * 1000.0;
                assert!(!resident.cache_hit);
                let resident_prov: Value = serde_json::from_str(
                    &fs::read_to_string(&resident.provenance_path).map_err(io_error)?,
                )
                .map_err(|_| CoreImageError::new("HARNESS_JSON", "invalid private provenance"))?;
                let pid = owner.pid(&slot.acquire()?);
                let pid_reused = previous_pid.map(|previous| pid == Some(previous));
                previous_pid = pid;
                let child_rss = pid.and_then(rss_kib);
                let expected_input = resolve_arw(&roots[folder], &cfg.captures[capture])?;
                let input_matches = |provenance: &Value| {
                    provenance["request"]["input"].as_str().map(Path::new)
                        == Some(expected_input.as_path())
                };
                if let Some(rss) = child_rss {
                    boundary_rss.push(rss as f64);
                }
                let class = match series.class {
                    transport_bench::SeriesClass::DecodeChanging => "decode_changing",
                    transport_bench::SeriesClass::PostOnly => "post_only",
                };
                let axes = |executor| json!({"executor": executor, "binary": binary, "host": "cargo_harness"});
                let mut single_keys: Vec<_> = single_prov.as_object().unwrap().keys().collect();
                let mut resident_keys: Vec<_> = resident_prov.as_object().unwrap().keys().collect();
                single_keys.sort();
                resident_keys.sort();
                records.push(json!({
                    "series": series.name, "class": class, "step": step, "round": round,
                    "folder_index": folder, "capture_index": capture,
                    "input_matches_expected": input_matches(&single_prov) && input_matches(&resident_prov),
                    "request_equal": single_prov["request"] == resident_prov["request"],
                    "cache_key_equal": single.params_hash == resident.params_hash,
                    "provenance_keys_equal": single_keys == resident_keys,
                    "bytes_identical": fs::read(&reference).map_err(io_error)? == fs::read(&resident.output_path).map_err(io_error)?,
                    "comparison": compare_rgb(&reference, Path::new(&resident.output_path)),
                    "single": {"axes": axes("single"), "wall_ms": single_ms, "cli_elapsed_ms": single.elapsed_ms,
                        "child_request_boundary_rss_kib": null},
                    "resident": {"axes": axes("resident"), "wall_ms": resident_ms, "cli_elapsed_ms": resident.elapsed_ms,
                        "child_request_boundary_rss_kib": child_rss,
                        "pid_observed": pid.is_some(), "pid_reused_from_previous": pid_reused},
                    "cargo_request_boundary_rss_kib": rss_kib(std::process::id()),
                }));
                fs::remove_file(reference).map_err(io_error)?;
                serial += 1;
            }
        }
    }
    let pid = owner.pid(&slot.acquire()?);
    owner.close()?;
    let child_reaped = pid.is_some_and(|pid| {
        !Command::new("kill")
            .args(["-0", &pid.to_string()])
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|s| s.success())
    });
    let mid = boundary_rss.len() / 2;
    let report = json!({
        "schema": 1, "axes": {"executor": ["single", "resident"], "binary": binary, "host": "cargo_harness"},
        "definitions": {
            "wall": "Rust develop miss incl resolve/cache key/provenance/prune; no Tauri/React/WebView",
            "request": "same JSON incl output, decoder 9/apple_default/lens correction/2400 JPEG; no op/handle",
            "order": "single then resident; two explicit RAWs alternate, folder switches every two requests",
            "memory": "ps RSS at request boundary in KiB; not peak RSS or footprint; first/second half are descriptive",
            "concurrency": "one active develop slot; idle resident and transient single CLI may coexist",
            "thresholds": "no tuned thresholds; exact bytes and decoded RGB equality required",
            "input": "both observed request inputs equal expected canonical root plus explicit capture ID; boolean only",
            "pid_reuse": "resident pid observed each request; previous-pid equality boolean, null only on first record",
            "privacy": "no paths, GPS, source-path hashes, request/provenance values in public report"
        },
        "memory_trend": {"samples": boundary_rss.len(), "first_half_rss_kib": stats(&boundary_rss[..mid]),
            "second_half_rss_kib": stats(&boundary_rss[mid..]), "true_peak_rss_bytes": null, "peak_footprint_bytes": null},
        "unmeasured": ["true process peak RSS", "GUI E2E", "IPC", "WebView display", "production TTL/recycle thresholds"],
        "child_reaped_after_eof_close": child_reaped,
        "records": records,
    });
    fs::write(
        out.join("resident-comparison.json"),
        serde_json::to_string_pretty(&report).unwrap() + "\n",
    )
    .map_err(io_error)?;
    // Private provenance includes input paths. Delete task-owned app_data after comparisons.
    fs::remove_dir_all(app).map_err(io_error)?;
    Ok(report)
}

/// Pure acceptance gate: usable for archived schema-1 reports without rerendering RAWs.
/// New input/PID observations have a separate strict gate, so legacy parity is not evidence for them.
fn validate_real_report(report: &Value, rounds: usize) -> Result<(), String> {
    let require = |ok: bool, message: &str| {
        if ok {
            Ok(())
        } else {
            Err(message.to_owned())
        }
    };
    require(
        rounds >= 4,
        "at least four explicit folder/capture rounds required",
    )?;
    require(report["schema"] == 1, "unsupported or missing schema")?;
    require(
        report["axes"]["binary"] == "real_cli",
        "real CLI report required",
    )?;
    require(
        report["child_reaped_after_eof_close"] == true,
        "child not proven reaped",
    )?;
    let records = report["records"]
        .as_array()
        .ok_or("missing records array")?;
    let series = standard_series();
    let steps: usize = series.iter().map(|s| s.steps.len()).sum();
    let expected = steps.checked_mul(rounds).ok_or("record count overflow")?;
    require(
        expected > 0 && records.len() == expected,
        "unexpected record count",
    )?;
    let mut index = 0;
    for series in series {
        for step in 0..series.steps.len() {
            for round in 0..rounds {
                let record = &records[index];
                let validate = || -> Result<(), String> {
                    require(record["series"] == series.name, "wrong or missing series")?;
                    require(
                        record["step"] == step && record["round"] == round,
                        "wrong or missing step/round",
                    )?;
                    require(
                        record["folder_index"] == (round / 2) % 2
                            && record["capture_index"] == round % 2,
                        "wrong or missing folder/capture index",
                    )?;
                    for key in [
                        "request_equal",
                        "cache_key_equal",
                        "provenance_keys_equal",
                        "bytes_identical",
                    ] {
                        require(record[key] == true, &format!("{key} must be true"))?;
                    }
                    let comparison = &record["comparison"];
                    for key in ["comparable", "bytes_identical", "rawpixels_identical"] {
                        require(
                            comparison[key] == true,
                            &format!("comparison.{key} must be true"),
                        )?;
                    }
                    // compare_rgb emits width/height only after verifying equal decoded sizes.
                    let width = comparison["width"]
                        .as_u64()
                        .ok_or("missing positive width")?;
                    let height = comparison["height"]
                        .as_u64()
                        .ok_or("missing positive height")?;
                    require(
                        width > 0 && height > 0 && width.max(height) == PREVIEW_MAX_EDGE as u64,
                        "equal positive 2400-long-edge dimensions required",
                    )?;
                    require(
                        comparison["max_abs_diff"].as_f64() == Some(0.0),
                        "nonzero or missing max_abs_diff",
                    )?;
                    require(
                        comparison["differing_pixels"].as_u64() == Some(0),
                        "nonzero or missing differing_pixels",
                    )?;
                    require(
                        comparison["pixel_count"].as_u64() == Some(width * height),
                        "wrong or missing pixel_count",
                    )?;
                    Ok(())
                };
                validate().map_err(|error| format!("record {index}: {error}"))?;
                index += 1;
            }
        }
    }
    Ok(())
}

fn validate_observations(report: &Value) -> Result<(), String> {
    let records = report["records"]
        .as_array()
        .filter(|r| !r.is_empty())
        .ok_or("missing observations")?;
    for (index, record) in records.iter().enumerate() {
        if record["input_matches_expected"] != true || record["resident"]["pid_observed"] != true {
            return Err(format!(
                "record {index}: expected input and resident pid must be observed"
            ));
        }
        let reuse = record["resident"].get("pid_reused_from_previous");
        // Default policy has no recycle: every request after the first must reuse the child.
        if reuse != Some(&if index == 0 { Value::Null } else { json!(true) }) {
            return Err(format!(
                "record {index}: missing or unexpected pid reuse observation"
            ));
        }
    }
    Ok(())
}

#[test]
#[ignore = "saved JSON parity only; no CLI/RAW execution, input/PID observations not assessed"]
fn resident_saved_report_parity() {
    let path = std::env::var("GEOFRAME_RESIDENT_REPORT").expect("REPORT path required");
    let rounds =
        std::env::var("GEOFRAME_RESIDENT_ROUNDS").map_or(4, |v| v.parse().expect("ROUNDS"));
    let report: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    validate_real_report(&report, rounds).unwrap();
    eprintln!(
        "saved JSON parity passed; input/PID observations are not assessed by this entry point"
    );
}

// Synthetic acceptance fixtures are independent of the deliberately non-JPEG mock renderer.
fn accepted_report() -> Value {
    let mut records = vec![];
    for series in standard_series() {
        for step in 0..series.steps.len() {
            for round in 0..4 {
                records.push(json!({
                    "series": series.name, "step": step, "round": round,
                    "folder_index": (round / 2) % 2, "capture_index": round % 2,
                    "request_equal": true, "cache_key_equal": true, "provenance_keys_equal": true,
                    "bytes_identical": true, "input_matches_expected": true,
                    "resident": {"pid_observed": true, "pid_reused_from_previous": if records.is_empty() { Value::Null } else { json!(true) }},
                    "comparison": {"comparable": true, "width": 2400, "height": 1600,
                        "bytes_identical": true, "rawpixels_identical": true, "max_abs_diff": 0,
                        "differing_pixels": 0, "pixel_count": 3840000},
                }));
            }
        }
    }
    json!({"schema": 1, "axes": {"binary": "real_cli"}, "child_reaped_after_eof_close": true, "records": records})
}

#[test]
fn real_report_gate_accepts_exact_parity_and_rejects_each_missing_or_false_gate() {
    let accepted = accepted_report();
    assert_eq!(accepted["records"].as_array().unwrap().len(), 48 * 4);
    validate_real_report(&accepted, 4).unwrap();
    for pointer in [
        "/child_reaped_after_eof_close",
        "/records/0/request_equal",
        "/records/0/cache_key_equal",
        "/records/0/provenance_keys_equal",
        "/records/0/bytes_identical",
        "/records/0/comparison/comparable",
        "/records/0/comparison/bytes_identical",
        "/records/0/comparison/rawpixels_identical",
    ] {
        for bad in [json!(false), Value::Null, json!("true")] {
            let mut report = accepted.clone();
            *report.pointer_mut(pointer).unwrap() = bad;
            assert!(validate_real_report(&report, 4).is_err(), "{pointer}");
        }
        let mut report = accepted.clone();
        let (parent, key) = pointer.rsplit_once('/').unwrap();
        report
            .pointer_mut(parent)
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove(key);
        assert!(
            validate_real_report(&report, 4).is_err(),
            "missing {pointer}"
        );
    }
    for key in [
        "width",
        "height",
        "max_abs_diff",
        "differing_pixels",
        "pixel_count",
    ] {
        let mut report = accepted.clone();
        report["records"][0]["comparison"]
            .as_object_mut()
            .unwrap()
            .remove(key);
        assert!(validate_real_report(&report, 4).is_err(), "missing {key}");
    }
}

#[test]
fn real_report_gate_rejects_empty_incomplete_incomparable_or_different_reports() {
    let accepted = accepted_report();
    for (pointer, bad) in [
        ("/records", json!([])),
        ("/records", Value::Null),
        (
            "/records/0/comparison",
            json!({"comparable": false, "reason": "dimension mismatch"}),
        ),
        ("/records/0/comparison/width", json!(0)),
        ("/records/0/comparison/height", json!(0)),
        ("/records/0/comparison/width", json!(1200)),
        ("/records/0/comparison/height", json!(2401)),
        ("/records/0/comparison/max_abs_diff", json!(1)),
        ("/records/0/comparison/differing_pixels", json!(1)),
        ("/records/0/comparison/pixel_count", json!(0)),
        ("/records/0/step", json!(1)),
        ("/records/0/round", json!(1)),
        ("/records/0/series", json!("unknown")),
        ("/records/0/folder_index", Value::Null),
        ("/records/0/capture_index", json!(1)),
        ("/schema", Value::Null),
        ("/axes/binary", json!("mock")),
    ] {
        let mut report = accepted.clone();
        *report.pointer_mut(pointer).unwrap() = bad;
        assert!(validate_real_report(&report, 4).is_err(), "{pointer}");
    }
    let mut report = accepted.clone();
    report["records"].as_array_mut().unwrap().pop();
    assert!(validate_real_report(&report, 4).is_err());
    let mut report = accepted.clone();
    report["records"]
        .as_array_mut()
        .unwrap()
        .push(accepted["records"][0].clone());
    assert!(validate_real_report(&report, 4).is_err());
    assert!(validate_real_report(&accepted, 0).is_err());
    assert!(validate_real_report(&accepted, 5).is_err());
}

#[test]
fn observations_gate_requires_input_and_pid_evidence_without_legacy_defaults() {
    let accepted = accepted_report();
    validate_observations(&accepted).unwrap();
    for pointer in [
        "/records/0/input_matches_expected",
        "/records/0/resident/pid_observed",
        "/records/0/resident/pid_reused_from_previous",
        "/records/1/resident/pid_reused_from_previous",
    ] {
        let mut report = accepted.clone();
        *report.pointer_mut(pointer).unwrap() = json!(false);
        assert!(validate_observations(&report).is_err(), "{pointer}");
        let mut report = accepted.clone();
        let (parent, key) = pointer.rsplit_once('/').unwrap();
        report
            .pointer_mut(parent)
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove(key);
        assert!(validate_observations(&report).is_err(), "missing {pointer}");
    }
    let mut legacy = accepted.clone();
    for record in legacy["records"].as_array_mut().unwrap() {
        record
            .as_object_mut()
            .unwrap()
            .remove("input_matches_expected");
        record.as_object_mut().unwrap().remove("resident");
    }
    validate_real_report(&legacy, 4).unwrap();
    assert!(validate_observations(&legacy).is_err());
    assert!(validate_observations(&json!({"records": []})).is_err());
}

#[test]
#[ignore = "real CLI/RAW measurement is run by parent; see docs/reports/coreimage-resident-pr-a.md"]
fn resident_real_cli_comparison() {
    assert!(
        !cfg!(debug_assertions),
        "use --release for real measurements"
    );
    let need = |name| std::env::var(format!("GEOFRAME_RESIDENT_{name}")).expect(name);
    let cfg = Config {
        sidecar: PathBuf::from(need("SIDECAR")),
        roots: [
            PathBuf::from(need("SOURCE_ROOT_A")),
            PathBuf::from(need("SOURCE_ROOT_B")),
        ],
        captures: [need("CAPTURE_A"), need("CAPTURE_B")],
        out: PathBuf::from(need("OUT")),
        rounds: std::env::var("GEOFRAME_RESIDENT_ROUNDS").map_or(4, |v| v.parse().expect("ROUNDS")),
        timeout: DEVELOP_TIMEOUT,
    };
    assert!(cfg.rounds >= 4 && cfg.captures[0] != cfg.captures[1]);
    assert!(
        fs::metadata(&cfg.sidecar).unwrap().len() > 100_000,
        "real built CLI required"
    );
    let report = run(&cfg, "real_cli").unwrap();
    validate_real_report(&report, cfg.rounds).unwrap();
    validate_observations(&report).unwrap();
}

#[test]
fn mock_harness_reports_axes_order_privacy_and_memory_definitions() {
    use super::tests::{mock, Dir};
    let dir = Dir::new();
    let sidecar = mock(dir.path());
    let roots = [dir.path().join("source-a"), dir.path().join("source-b")];
    for (i, root) in roots.iter().enumerate() {
        fs::create_dir(root).unwrap();
        for capture in ["DSC04430", "DSC04880"] {
            fs::write(
                root.join(format!("{capture}.ARW")),
                format!("{i}-{capture}"),
            )
            .unwrap();
        }
    }
    let cfg = Config {
        sidecar,
        roots,
        captures: ["DSC04430".into(), "DSC04880".into()],
        out: dir.path().join("scratch"),
        rounds: 4,
        timeout: Duration::from_secs(5),
    };
    let report = run(&cfg, "mock").unwrap();
    let text = report.to_string();
    assert!(!text.contains(dir.path().to_str().unwrap()));
    assert!(!text.contains("source_path_hash"));
    assert_eq!(report["axes"]["binary"], "mock");
    assert_eq!(report["axes"]["host"], "cargo_harness");
    assert_eq!(report["memory_trend"]["true_peak_rss_bytes"], Value::Null);
    assert_eq!(report["child_reaped_after_eof_close"], true);
    let records = report["records"].as_array().unwrap();
    assert_eq!(records.len(), 192);
    for (i, record) in records.iter().enumerate() {
        assert_eq!(record["input_matches_expected"], true);
        assert_eq!(record["resident"]["pid_observed"], true);
        assert_eq!(
            record["resident"]["pid_reused_from_previous"],
            if i == 0 { Value::Null } else { json!(true) }
        );
        assert_eq!(record["folder_index"], (i / 2) % 2);
        assert_eq!(record["capture_index"], i % 2);
        assert_eq!(record["bytes_identical"], true);
        assert_eq!(record["request_equal"], true);
        assert_eq!(record["cache_key_equal"], true);
        assert_eq!(record["provenance_keys_equal"], true);
    }
    // Mock payloads are intentionally not JPEGs: their decode failures cannot claim real quality.
    assert_eq!(records[0]["comparison"]["comparable"], false);
    assert!(!cfg.out.join("app_data").exists());
    assert_eq!(
        prepare(&cfg).unwrap_err().code,
        "HARNESS_IO",
        "never reuse OUT"
    );
}

#[test]
fn harness_output_protects_source_and_symlink_parent() {
    use super::tests::{mock, Dir};
    let dir = Dir::new();
    let sidecar = mock(dir.path());
    let source = dir.path().join("source");
    fs::create_dir(&source).unwrap();
    let link = dir.path().join("link");
    std::os::unix::fs::symlink(&source, &link).unwrap();
    let mut cfg = Config {
        sidecar,
        roots: [source.clone(), source.clone()],
        captures: ["DSC04430".into(), "DSC04880".into()],
        out: link.join("scratch"),
        rounds: 4,
        timeout: DEVELOP_TIMEOUT,
    };
    assert_eq!(prepare(&cfg).unwrap_err().code, "HARNESS_OUTPUT");
    assert!(!source.join("scratch").exists());
    cfg.out = dir.path().to_path_buf();
    assert_eq!(prepare(&cfg).unwrap_err().code, "HARNESS_OUTPUT");
}
