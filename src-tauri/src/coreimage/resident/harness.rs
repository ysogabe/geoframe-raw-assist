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
    let owner = ResidentOwner::new(&cfg.sidecar, Policy::default())?;
    let mut records = vec![];
    let mut boundary_rss = vec![];
    let mut serial = 0;
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
                let child_rss = owner.pid(&slot.acquire()?).and_then(rss_kib);
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
                    "request_equal": single_prov["request"] == resident_prov["request"],
                    "cache_key_equal": single.params_hash == resident.params_hash,
                    "provenance_keys_equal": single_keys == resident_keys,
                    "bytes_identical": fs::read(&reference).map_err(io_error)? == fs::read(&resident.output_path).map_err(io_error)?,
                    "comparison": compare_rgb(&reference, Path::new(&resident.output_path)),
                    "single": {"axes": axes("single"), "wall_ms": single_ms, "cli_elapsed_ms": single.elapsed_ms,
                        "child_request_boundary_rss_kib": null},
                    "resident": {"axes": axes("resident"), "wall_ms": resident_ms, "cli_elapsed_ms": resident.elapsed_ms,
                        "child_request_boundary_rss_kib": child_rss},
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
            "thresholds": "none tuned; exact bytes and decoded RGB comparison recorded",
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
    assert_eq!(report["child_reaped_after_eof_close"], true);
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
