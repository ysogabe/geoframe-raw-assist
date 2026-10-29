//! Mock-CLI tests for the QA wiring. They prove Rust-side routing, evidence, lifecycle and slot
//! behavior only; they say nothing about the real app, GUI, real CLI timing or process exit.
use super::super::tests::{alive, lines, mock, request, wait_until, Dir};
use super::*;

struct Env {
    dir: Dir,
    cli: PathBuf,
    app: PathBuf,
}

fn env() -> Env {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let root = dir.path().join("root");
    fs::create_dir(&root).unwrap();
    for id in ["DSC04430", "DSC04880"] {
        fs::write(root.join(format!("{id}.ARW")), format!("raw-{id}")).unwrap();
    }
    Env {
        app: app_dir(dir.path(), "app"),
        dir,
        cli,
    }
}

/// An app-data directory whose config points at the shared `root` of `dir`.
fn app_dir(dir: &Path, name: &str) -> PathBuf {
    let app = dir.join(name);
    fs::create_dir(&app).unwrap();
    fs::write(
        app.join("config.json"),
        json!({"source_root": dir.join("root")}).to_string(),
    )
    .unwrap();
    app
}

fn adj(exposure: f64) -> Value {
    let mut value = transport_bench::base_adjustments();
    value["exposure"] = json!(exposure);
    crate::adjustments::normalize_adjustments(&value).unwrap()
}

fn prov(result: &CoreImagePreviewResult) -> Value {
    serde_json::from_str(&fs::read_to_string(&result.provenance_path).unwrap()).unwrap()
}

fn preview(
    qa: &ResidentPreview<'_>,
    env: &Env,
    id: &str,
    mode: DevelopMode,
    exposure: f64,
) -> Result<CoreImagePreviewResult, CoreImageError> {
    qa.develop(
        &env.app,
        &env.cli,
        id,
        mode,
        Some(&adj(exposure)),
        Duration::from_secs(5),
    )
}

fn single(
    slot: &DevelopSlot,
    env: &Env,
    app: &Path,
    id: &str,
    exposure: f64,
) -> Result<CoreImagePreviewResult, CoreImageError> {
    develop(
        app,
        &env.cli,
        id,
        DevelopMode::Preview,
        Some(&adj(exposure)),
        slot,
        Duration::from_secs(5),
    )
}

fn evidence_pid(provenance: &Value) -> u32 {
    let evidence = &provenance[EVIDENCE_KEY];
    assert_eq!(evidence["executor"], EVIDENCE_EXECUTOR);
    assert_eq!(evidence["schema"], 1);
    evidence["owner_pid"].as_u64().unwrap() as u32
}

fn owner_pid(qa: &ResidentPreview<'_>, env: &Env, slot: &DevelopSlot) -> Option<u32> {
    qa.pinned(&env.cli)
        .unwrap()
        .owner
        .pid(&slot.acquire().unwrap())
}

#[test]
fn qa_identifier_guard_accepts_only_the_dedicated_id() {
    assert!(check_identifier("com.geoframe.rawassist.qaroadmap").is_ok());
    for bad in [
        "com.geoframe.rawassist",
        "com.geoframe.rawassist.qaroadmap.x",
        "com.geoframe.rawassist.QAROADMAP",
        " com.geoframe.rawassist.qaroadmap",
        "",
    ] {
        let error = check_identifier(bad).unwrap_err();
        assert_eq!(error.code, "RESIDENT_QA_WRONG_APP_ID", "{bad:?}");
        assert!(error.message.contains("com.geoframe.rawassist.qaroadmap"));
    }
}

#[test]
fn preview_is_lazy_and_reused_while_full_stays_single() {
    let env = env();
    let slot = DevelopSlot::new(2);
    let qa = ResidentPreview::new(&slot, Policy::default());
    assert!(qa.pinned.lock().unwrap().is_none(), "no owner before use");
    assert!(lines(env.dir.path(), "starts").is_empty());

    let first = preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.0).unwrap();
    assert!(!first.cache_hit && first.preview);
    let pid = evidence_pid(&prov(&first));
    assert_eq!(lines(env.dir.path(), "starts"), vec![pid.to_string()]);
    let second = preview(&qa, &env, "DSC04880", DevelopMode::Preview, 0.5).unwrap();
    assert_eq!(evidence_pid(&prov(&second)), pid, "same resident child");
    assert_eq!(lines(env.dir.path(), "starts").len(), 1);

    let full = preview(&qa, &env, "DSC04430", DevelopMode::Full, 0.0).unwrap();
    assert!(!full.preview && !full.cache_hit);
    assert!(
        prov(&full).get(EVIDENCE_KEY).is_none(),
        "full is single-shot and carries no resident evidence"
    );
    let starts = lines(env.dir.path(), "starts");
    assert_eq!(starts.len(), 2, "full spawned its own one-shot process");
    assert_ne!(starts[1], pid.to_string());
    assert_eq!(owner_pid(&qa, &env, &slot), Some(pid), "idle resident kept");
    let resident_calls = lines(env.dir.path(), "calls")
        .iter()
        .filter(|line| serde_json::from_str::<Value>(line).unwrap()["pid"] == json!(pid))
        .count();
    assert_eq!(resident_calls, 2, "full never reached the resident child");
    assert_eq!(
        qa.pinned(&env.cli)
            .unwrap()
            .owner
            .state
            .lock()
            .unwrap()
            .requests,
        2
    );
}

#[test]
fn evidence_is_outside_request_and_cache_key() {
    let env = env();
    let slot = DevelopSlot::new(2);
    let qa = ResidentPreview::new(&slot, Policy::default());
    let other = app_dir(env.dir.path(), "app-single");
    let by_single = single(&slot, &env, &other, "DSC04430", 0.25).unwrap();
    let by_resident = preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.25).unwrap();
    assert_eq!(by_single.params_hash, by_resident.params_hash);
    let single_prov = prov(&by_single);
    let mut resident_prov = prov(&by_resident);
    assert!(single_prov.get(EVIDENCE_KEY).is_none());
    evidence_pid(&resident_prov);
    assert!(
        resident_prov["request"].get(EVIDENCE_KEY).is_none(),
        "evidence is not part of the raw request"
    );
    resident_prov.as_object_mut().unwrap().remove(EVIDENCE_KEY);
    // `remove` reorders under preserve_order, so compare as sets.
    let keys = |value: &Value| {
        value
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>()
    };
    assert_eq!(keys(&single_prov), keys(&resident_prov));
    assert_eq!(single_prov["width"], resident_prov["width"]);
    assert!(resident_prov.get("sequence").is_none());
}

#[test]
fn cache_hit_attribution_never_claims_a_resident_run() {
    let env = env();
    let slot = DevelopSlot::new(2);
    let qa = ResidentPreview::new(&slot, Policy::default());
    // Entry produced by the single-shot path.
    let seed = single(&slot, &env, &env.app, "DSC04430", 0.0).unwrap();
    assert!(!seed.cache_hit);
    let hit = preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.0).unwrap();
    assert!(hit.cache_hit);
    assert!(prov(&hit).get(EVIDENCE_KEY).is_none(), "Single-made entry");
    assert_eq!(owner_pid(&qa, &env, &slot), None, "no resident child ran");
    assert_eq!(lines(env.dir.path(), "starts").len(), 1, "only the seed");

    // Entry produced by the resident child keeps its own evidence on later hits, even after
    // that child is gone: the tag is a fact about the producing run, not about current liveness.
    let made = preview(&qa, &env, "DSC04430", DevelopMode::Preview, 1.0).unwrap();
    assert!(!made.cache_hit);
    let pid = evidence_pid(&prov(&made));
    qa.pinned(&env.cli).unwrap().owner.close().unwrap();
    assert!(!alive(pid));
    let again = preview(&qa, &env, "DSC04430", DevelopMode::Preview, 1.0).unwrap();
    assert!(again.cache_hit);
    assert_eq!(evidence_pid(&prov(&again)), pid);
    assert_eq!(lines(env.dir.path(), "starts").len(), 2, "no new child");
}

#[test]
fn resident_cache_hit_takes_a_slot_but_single_does_not() {
    let env = env();
    let slot = DevelopSlot::new(0);
    let qa = ResidentPreview::new(&slot, Policy::default());
    single(&slot, &env, &env.app, "DSC04430", 0.0).unwrap();
    let held = slot.acquire().unwrap();
    assert!(
        single(&slot, &env, &env.app, "DSC04430", 0.0)
            .unwrap()
            .cache_hit
    );
    assert_eq!(
        preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.0)
            .unwrap_err()
            .code,
        "BUSY",
        "identity check needs the slot even for a hit (documented trade-off)"
    );
    drop(held);
    assert!(
        preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.0)
            .unwrap()
            .cache_hit
    );
}

#[test]
fn reap_idle_closes_with_eof_and_next_preview_starts_again() {
    let env = env();
    let slot = DevelopSlot::new(2);
    let qa = ResidentPreview::new(
        &slot,
        Policy {
            idle_ttl: Some(Duration::ZERO),
            max_requests: None,
        },
    );
    assert!(!qa.reap_idle(), "nothing to reap before first use");
    let first = preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.0).unwrap();
    let pid = evidence_pid(&prov(&first));
    assert!(qa.reap_idle());
    wait_until(|| env.dir.path().join("eof").exists());
    assert!(!alive(pid));
    let second = preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.5).unwrap();
    assert_ne!(evidence_pid(&prov(&second)), pid);
    assert_eq!(lines(env.dir.path(), "starts").len(), 2);
}

#[test]
fn busy_owner_is_neither_reaped_nor_killed_and_shutdown_blocks_new_children() {
    let env = env();
    let slot = DevelopSlot::new(2);
    let qa = ResidentPreview::new(
        &slot,
        Policy {
            idle_ttl: None,
            max_requests: Some(0), // expired as soon as the lock is free; busy must still win
        },
    );
    let owner = qa.pinned(&env.cli).unwrap();
    let req = request(env.dir.path(), "block");
    thread::scope(|scope| {
        let active = scope.spawn(|| {
            owner.owner.request_observed(
                &env.cli,
                &req,
                Duration::from_secs(20),
                &slot.acquire().unwrap(),
            )
        });
        wait_until(|| lines(env.dir.path(), "calls").len() == 1);
        let pid: u32 = lines(env.dir.path(), "starts")[0].parse().unwrap();
        assert!(!qa.reap_idle());
        assert_eq!(
            qa.shutdown(Duration::from_millis(60)),
            ShutdownOutcome::StillBusy
        );
        assert!(alive(pid), "active child is never killed by cleanup");
        fs::write(env.dir.path().join("release"), b"go").unwrap();
        let (_, observed) = active.join().unwrap().unwrap();
        assert_eq!(observed, Some(pid));
    });
    assert_eq!(qa.shutdown(Duration::from_secs(2)), ShutdownOutcome::Closed);
    wait_until(|| env.dir.path().join("eof").exists());
    assert_eq!(
        preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.0)
            .unwrap_err()
            .code,
        "RESIDENT_CLOSED"
    );
    assert_eq!(
        owner
            .owner
            .request_observed(
                &env.cli,
                &req,
                Duration::from_secs(1),
                &slot.acquire().unwrap()
            )
            .unwrap_err()
            .code,
        "RESIDENT_CLOSED"
    );
    assert_eq!(
        lines(env.dir.path(), "starts").len(),
        1,
        "no child after shutdown"
    );
}

#[test]
fn shutdown_without_use_starts_nothing_and_refuses_later_previews() {
    let env = env();
    let slot = DevelopSlot::new(2);
    let qa = ResidentPreview::new(&slot, Policy::default());
    assert_eq!(qa.shutdown(Duration::ZERO), ShutdownOutcome::NotStarted);
    assert_eq!(
        preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.0)
            .unwrap_err()
            .code,
        "RESIDENT_CLOSED"
    );
    assert!(lines(env.dir.path(), "starts").is_empty());
    // Full is still served by the single-shot path.
    assert!(preview(&qa, &env, "DSC04430", DevelopMode::Full, 0.0).is_ok());
}

#[test]
fn background_reaper_closes_idle_child_and_app_shutdown_joins_it() {
    let env = env();
    let slot: &'static DevelopSlot = Box::leak(Box::new(DevelopSlot::new(2)));
    let app = QaApp::start(
        slot,
        Policy {
            idle_ttl: Some(Duration::from_millis(30)),
            max_requests: None,
        },
        Duration::from_millis(10),
    );
    let made = app
        .preview()
        .develop(
            &env.app,
            &env.cli,
            "DSC04430",
            DevelopMode::Preview,
            Some(&adj(0.0)),
            Duration::from_secs(5),
        )
        .unwrap();
    let pid = evidence_pid(&prov(&made));
    wait_until(|| !alive(pid));
    assert!(
        env.dir.path().join("eof").exists(),
        "closed by EOF, not by kill"
    );
    assert_eq!(
        app.shutdown(Duration::from_secs(2)),
        ShutdownOutcome::Closed
    );
    assert!(app.reaper.lock().unwrap().join.is_none(), "reaper joined");
    assert_eq!(
        app.shutdown(Duration::from_secs(2)),
        ShutdownOutcome::Closed,
        "idempotent"
    );
}

#[test]
fn app_shutdown_closes_the_idle_child_with_eof() {
    let env = env();
    let slot: &'static DevelopSlot = Box::leak(Box::new(DevelopSlot::new(2)));
    let app = QaApp::start(slot, Policy::default(), Duration::from_millis(10));
    let made = app
        .preview()
        .develop(
            &env.app,
            &env.cli,
            "DSC04880",
            DevelopMode::Preview,
            Some(&adj(0.0)),
            Duration::from_secs(5),
        )
        .unwrap();
    let pid = evidence_pid(&prov(&made));
    assert!(alive(pid));
    assert_eq!(
        app.shutdown(Duration::from_secs(2)),
        ShutdownOutcome::Closed
    );
    assert!(!alive(pid));
    assert!(env.dir.path().join("eof").exists());
}

#[test]
fn default_knobs_are_the_documented_uncalibrated_qa_values() {
    assert_eq!(QA_IDLE_TTL, Duration::from_secs(90));
    assert_eq!(QA_MAX_REQUESTS, 64);
    let app = QaApp::start_default();
    assert_eq!(
        app.shutdown(Duration::ZERO),
        ShutdownOutcome::NotStarted,
        "start_default does not spawn a child"
    );
}

#[test]
fn sidecar_identity_change_and_second_path_stay_rejected() {
    let env = env();
    let slot = DevelopSlot::new(2);
    let qa = ResidentPreview::new(&slot, Policy::default());
    preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.0).unwrap();
    let original = fs::read(&env.cli).unwrap();
    fs::write(&env.cli, [original.as_slice(), b"\n# new"].concat()).unwrap();
    let error = preview(&qa, &env, "DSC04430", DevelopMode::Preview, 0.5).unwrap_err();
    assert_eq!(error.code, "SIDECAR_IDENTITY_CHANGED");
    // Restoring the bytes does not reopen the path: the owner stays invalid until app restart.
    fs::write(&env.cli, &original).unwrap();
    assert_eq!(
        preview(&qa, &env, "DSC04880", DevelopMode::Preview, 0.0)
            .unwrap_err()
            .code,
        "SIDECAR_IDENTITY_CHANGED"
    );
    assert_eq!(
        lines(env.dir.path(), "starts").len(),
        1,
        "no silent re-creation"
    );
    let elsewhere = env.dir.path().join("other-cli");
    fs::copy(&env.cli, &elsewhere).unwrap();
    let error = qa
        .develop(
            &env.app,
            &elsewhere,
            "DSC04430",
            DevelopMode::Preview,
            Some(&adj(0.0)),
            Duration::from_secs(5),
        )
        .unwrap_err();
    assert_eq!(error.code, "SIDECAR_IDENTITY_CHANGED");
}
