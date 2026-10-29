use super::*;
use std::os::unix::fs::PermissionsExt;
use std::sync::Arc;

pub(super) struct Dir(PathBuf);
impl Dir {
    pub(super) fn new() -> Self {
        static N: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "geoframe-resident-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir_all(&path).unwrap();
        Self(path.canonicalize().unwrap())
    }
    pub(super) fn path(&self) -> &Path {
        &self.0
    }
}
impl Drop for Dir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

// Mock binary only: records every received line before any injected failure. No image quality
// or real CLI timing claim may be derived from this fixture.
pub(super) fn mock(dir: &Path) -> PathBuf {
    let program = dir.join("mock-cli");
    fs::write(&program, r##"#!/usr/bin/python3
import sys, json, os, time
from pathlib import Path
root = Path(__file__).parent
if '--version' in sys.argv:
    print(json.dumps(dict(ok=True, cli_version='mock', macos_version='mock')))
    sys.exit(0)
batch = '--batch' in sys.argv
with open(root / 'starts', 'a') as f: f.write(str(os.getpid()) + '\n')
def run(req, n):
    with open(root / 'calls', 'a') as f: f.write(json.dumps(dict(pid=os.getpid(), request=req)) + '\n')
    action = req.get('test_action', 'ok')
    if action == 'hang': time.sleep(30)
    if action == 'block':
        deadline = time.monotonic() + 15
        while not (root / 'release').exists():
            if time.monotonic() >= deadline: raise RuntimeError('release barrier timed out')
            time.sleep(0.005)
    if action == 'die': sys.exit(3)
    if action == 'garbage':
        print('garbage', flush=True)
        return
    if action == 'badshape':
        print(json.dumps(dict(sequence=n)), flush=True)
        return
    if action in ['fail', 'badcode']:
        res = dict(ok=False, error=dict(code='SIDECAR_BAD_RESPONSE' if action == 'badcode' else 'RENDER_FAILED', message='original message'))
    else:
        # The payload depends on *each* request's input and adjustments, not previous state.
        Path(req['output']).write_bytes(Path(req['input']).read_bytes() + json.dumps(req.get('adjustments'), sort_keys=True).encode())
        res = dict(ok=True, output=req['output'], width=2400, height=1600, cli_version='mock',
                   macos_version='mock', decoder_version='9', elapsed_ms=dict(total=1), applied={}, source={})
        if action == 'badoutput': res['width'] = 0
    if batch: res['sequence'] = n + (7 if action == 'skew' else 0)
    if action == 'close_stdin': os.close(0)
    print(json.dumps(res), flush=True)
    if action == 'close_stdin': time.sleep(30)
if batch:
    for n, line in enumerate(sys.stdin, 1): run(json.loads(line), n)
    (root / 'eof').write_text(str(os.getpid()))
else:
    run(json.load(sys.stdin), 1)
"##).unwrap();
    fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
    program
}

pub(super) fn request(dir: &Path, action: &str) -> Value {
    let input = dir.join("DSC00001.ARW");
    fs::write(&input, b"mock raw").unwrap();
    let mut req = Value::Object(base_request(&input, DevelopMode::Preview, None));
    req["output"] = json!(dir.join("out.jpg"));
    req["test_action"] = json!(action);
    req
}
pub(super) fn lines(dir: &Path, file: &str) -> Vec<String> {
    fs::read_to_string(dir.join(file))
        .unwrap_or_default()
        .lines()
        .map(str::to_owned)
        .collect()
}
pub(super) fn alive(pid: u32) -> bool {
    Command::new("kill")
        .args(["-0", &pid.to_string()])
        .stderr(Stdio::null())
        .status()
        .unwrap()
        .success()
}
fn kill(pid: u32) {
    assert!(Command::new("kill")
        .args(["-KILL", &pid.to_string()])
        .status()
        .unwrap()
        .success());
}
pub(super) fn wait_until(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !predicate() {
        assert!(Instant::now() < deadline, "condition timed out");
        thread::sleep(Duration::from_millis(5));
    }
}
fn run(
    owner: &ResidentOwner<'_>,
    cli: &Path,
    req: &Value,
    slot: &DevelopSlot,
) -> Result<Value, CoreImageError> {
    let timeout = if req["test_action"] == "block" {
        Duration::from_secs(20) // Longer than the fixture barrier deadline, only for explicit holds.
    } else {
        Duration::from_secs(5)
    };
    owner.request(cli, req, timeout, &slot.acquire()?)
}

#[test]
fn reuse_stateless_requests_and_reject_ops_before_send() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    let req = request(dir.path(), "ok");
    for _ in 0..3 {
        let response = run(&owner, &cli, &req, &slot).unwrap();
        assert!(response.get("sequence").is_none());
    }
    for key in ["op", "handle"] {
        let mut bad = req.clone();
        bad[key] = json!("decode");
        assert_eq!(
            run(&owner, &cli, &bad, &slot).unwrap_err().code,
            "INVALID_REQUEST"
        );
    }
    let mut full = req.clone();
    full["mode"] = json!("full");
    assert_eq!(
        run(&owner, &cli, &full, &slot).unwrap_err().code,
        "INVALID_REQUEST"
    );
    assert_eq!(lines(dir.path(), "calls").len(), 3);
    assert_eq!(lines(dir.path(), "starts").len(), 1);
    for line in lines(dir.path(), "calls") {
        let value: Value = serde_json::from_str(&line).unwrap();
        assert!(value["request"].get("op").is_none());
        assert!(value["request"].get("handle").is_none());
    }
}

#[test]
fn timeout_is_one_attempt_reaps_old_pid_and_next_request_recovers() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    let old = owner.pid(&slot.acquire().unwrap()).unwrap();
    let error = owner
        .request(
            &cli,
            &request(dir.path(), "hang"),
            Duration::from_millis(100),
            &slot.acquire().unwrap(),
        )
        .unwrap_err();
    assert_eq!(error.code, "SIDECAR_TIMEOUT");
    assert!(!alive(old));
    assert_eq!(lines(dir.path(), "calls").len(), 2);
    assert_eq!(lines(dir.path(), "starts").len(), 1);
    assert_eq!(owner.pid(&slot.acquire().unwrap()), None);
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert_ne!(owner.pid(&slot.acquire().unwrap()), Some(old));
    assert_eq!(lines(dir.path(), "calls").len(), 3);
}

#[test]
fn uncertain_send_error_is_not_retried_without_sent_increment() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    run(&owner, &cli, &request(dir.path(), "close_stdin"), &slot).unwrap();
    let old = owner.pid(&slot.acquire().unwrap()).unwrap();
    // Child closed its pipe before replying, but remains alive. This exercises real EPIPE
    // (write_all failure), not a sent-count heuristic or an artificial retry fixture.
    let err = run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap_err();
    assert_eq!(err.code, "SIDECAR_IO_FAILED");
    assert_eq!(lines(dir.path(), "starts").len(), 1);
    assert_eq!(lines(dir.path(), "calls").len(), 1);
    assert!(!alive(old));
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert_eq!(lines(dir.path(), "starts").len(), 2);
}

#[test]
fn idle_death_and_inflight_failures_recover_only_on_next_call() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    let old = owner.pid(&slot.acquire().unwrap()).unwrap();
    kill(old);
    wait_until(|| {
        !owner
            .state
            .lock()
            .unwrap()
            .session
            .as_mut()
            .unwrap()
            .is_alive()
            .unwrap()
    });
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert_ne!(owner.pid(&slot.acquire().unwrap()), Some(old));
    for action in ["die", "skew", "garbage", "badshape"] {
        let before = lines(dir.path(), "calls").len();
        assert_eq!(
            run(&owner, &cli, &request(dir.path(), action), &slot)
                .unwrap_err()
                .code,
            "SIDECAR_BAD_RESPONSE"
        );
        assert_eq!(lines(dir.path(), "calls").len(), before + 1);
        assert_eq!(owner.pid(&slot.acquire().unwrap()), None);
        run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    }
}

#[test]
fn structured_cli_error_preserves_code_message_and_session() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    let error = run(&owner, &cli, &request(dir.path(), "fail"), &slot).unwrap_err();
    assert_eq!(
        error,
        CoreImageError::new("RENDER_FAILED", "original message")
    );
    let pid = owner.pid(&slot.acquire().unwrap());
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert_eq!(owner.pid(&slot.acquire().unwrap()), pid);
    let error = run(&owner, &cli, &request(dir.path(), "badcode"), &slot).unwrap_err();
    assert_eq!(
        error,
        CoreImageError::new("SIDECAR_BAD_RESPONSE", "original message")
    );
    assert_eq!(
        owner.pid(&slot.acquire().unwrap()),
        pid,
        "a structured CLI error code is not a protocol error"
    );
}

#[test]
fn ttl_and_count_are_idle_recycle_knobs() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(
        &cli,
        Policy {
            idle_ttl: Some(Duration::from_secs(10)),
            max_requests: Some(2),
        },
        &slot,
    )
    .unwrap();
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    let first = owner.pid(&slot.acquire().unwrap()).unwrap();
    assert!(!owner.reap_idle());
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert_eq!(owner.pid(&slot.acquire().unwrap()), Some(first));
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert!(!alive(first));
    let second = owner.pid(&slot.acquire().unwrap()).unwrap();
    // Deterministic monotonic age injection avoids long/flaky sleeps.
    owner.state.lock().unwrap().last_used = Some(Instant::now() - Duration::from_secs(11));
    assert!(owner.reap_idle());
    assert!(!alive(second));
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert_ne!(owner.pid(&slot.acquire().unwrap()), Some(second));
}

#[test]
fn guarded_execution_busy_bound_and_reap_never_interrupts_active_work() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = Arc::new(DevelopSlot::new(2));
    let owner = Arc::new(
        ResidentOwner::new(
            &cli,
            Policy {
                idle_ttl: Some(Duration::ZERO),
                max_requests: None,
            },
            &slot,
        )
        .unwrap(),
    );
    let req = request(dir.path(), "block");
    thread::scope(|scope| {
        let start = scope.spawn(|| run(&owner, &cli, &req, &slot));
        wait_until(|| lines(dir.path(), "calls").len() == 1);
        assert!(!owner.reap_idle());
        assert_eq!(owner.close().unwrap_err().code, "BUSY");
        let second = scope.spawn(|| run(&owner, &cli, &req, &slot));
        let third = scope.spawn(|| run(&owner, &cli, &req, &slot));
        wait_until(|| slot.pending() == 3);
        assert_eq!(slot.acquire().err().unwrap().code, "BUSY");
        assert_eq!(
            lines(dir.path(), "calls").len(),
            1,
            "waiting slot cannot send"
        );
        fs::write(dir.path().join("release"), b"go").unwrap();
        start.join().unwrap().unwrap();
        second.join().unwrap().unwrap();
        third.join().unwrap().unwrap();
    });
    assert_eq!(slot.pending(), 0);
    assert_eq!(lines(dir.path(), "calls").len(), 3);
    assert!(owner.reap_idle());
}

#[test]
fn wrong_slot_rejects_before_owner_wait_without_send_and_correct_slot_recovers() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(0);
    let wrong_slot = DevelopSlot::new(0);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    let req = request(dir.path(), "block");
    thread::scope(|scope| {
        let active = scope.spawn(|| run(&owner, &cli, &req, &slot));
        wait_until(|| lines(dir.path(), "calls").len() == 1);
        let (send, receive) = std::sync::mpsc::channel();
        let owner = &owner;
        let cli = &cli;
        let req = &req;
        let wrong_slot = &wrong_slot;
        let wrong = scope.spawn(move || {
            let guard = wrong_slot.acquire().unwrap();
            let request_error = owner
                .request(&cli, &req, Duration::from_secs(5), &guard)
                .unwrap_err()
                .code;
            let identity_error = owner.validate_identity(&cli, &guard).unwrap_err().code;
            assert_eq!(owner.pid(&guard), None);
            owner.invalidate(&guard); // Wrong origin must not lock or evict the active session.
            send.send((request_error, identity_error)).unwrap();
        });
        let outcome = receive.recv_timeout(Duration::from_secs(1));
        let calls_while_blocked = lines(dir.path(), "calls").len();
        let pending_while_blocked = slot.pending();
        // Always release before asserting: a regression must fail without leaving a deadlock.
        fs::write(dir.path().join("release"), b"go").unwrap();
        active.join().unwrap().unwrap();
        wrong.join().unwrap();
        let (request_error, identity_error) = outcome.expect("wrong slot waited for owner mutex");
        assert_eq!(request_error, "INVALID_SLOT");
        assert_eq!(identity_error, "INVALID_SLOT");
        assert_eq!(calls_while_blocked, 1, "wrong slot must send zero requests");
        assert_eq!(
            pending_while_blocked, 1,
            "wrong slot must not join the bound slot queue"
        );
    });
    let pid = owner.pid(&slot.acquire().unwrap()).unwrap();
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert_eq!(owner.pid(&slot.acquire().unwrap()), Some(pid));
    assert_eq!(lines(dir.path(), "calls").len(), 2);
    assert_eq!(lines(dir.path(), "starts").len(), 1);
    assert_eq!(slot.pending(), 0);
    assert_eq!(wrong_slot.pending(), 0);
}

#[test]
fn stamp_change_is_sticky_even_after_close_and_new_owner_cannot_use_old_hash() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    let old = owner.pid(&slot.acquire().unwrap()).unwrap();
    let original = fs::read(&cli).unwrap();
    let replacement = dir.path().join("new-cli");
    let mut changed = original.clone();
    changed.extend(b"\n# replacement\n");
    fs::write(&replacement, changed).unwrap();
    fs::set_permissions(&replacement, fs::Permissions::from_mode(0o755)).unwrap();
    fs::rename(&replacement, &cli).unwrap();
    assert_eq!(
        run(&owner, &cli, &request(dir.path(), "ok"), &slot)
            .unwrap_err()
            .code,
        "SIDECAR_IDENTITY_CHANGED"
    );
    assert!(!alive(old));
    assert_eq!(lines(dir.path(), "calls").len(), 1);
    owner.close().unwrap();
    assert!(
        matches!(ResidentOwner::new(&cli, Policy::default(), &slot), Err(e) if e.code == "SIDECAR_IDENTITY_CHANGED")
    );
    fs::write(&cli, original).unwrap();
    assert_eq!(
        run(&owner, &cli, &request(dir.path(), "ok"), &slot)
            .unwrap_err()
            .code,
        "SIDECAR_IDENTITY_CHANGED"
    );
}

#[test]
fn poison_recovery_discards_old_session() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    let old = owner.pid(&slot.acquire().unwrap()).unwrap();
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _guard = slot.acquire().unwrap();
        let _state = owner.state.lock().unwrap();
        panic!("poison owner");
    }));
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    assert!(!alive(old));
    assert_ne!(owner.pid(&slot.acquire().unwrap()), Some(old));
    assert!(!owner.state.is_poisoned());
}

#[test]
fn drop_and_close_deliver_eof_and_reap_even_when_eof_is_ignored() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    let pid = owner.pid(&slot.acquire().unwrap()).unwrap();
    let started = Instant::now();
    drop(owner);
    assert!(started.elapsed() < Duration::from_secs(2));
    assert!(!alive(pid));
    assert_eq!(
        fs::read_to_string(dir.path().join("eof")).unwrap(),
        pid.to_string()
    );
    let stubborn = dir.path().join("ignore-eof");
    fs::write(&stubborn, "#!/bin/sh\nexec sleep 30\n").unwrap();
    fs::set_permissions(&stubborn, fs::Permissions::from_mode(0o755)).unwrap();
    let owner = ResidentOwner::new(&stubborn, Policy::default(), &slot).unwrap();
    owner.state.lock().unwrap().session =
        Some(batch::BatchSession::start(&stubborn, Duration::from_secs(1)).unwrap());
    let pid = owner.pid(&slot.acquire().unwrap()).unwrap();
    let started = Instant::now();
    owner.close().unwrap();
    assert!(started.elapsed() < Duration::from_secs(2));
    assert!(!alive(pid));
}

#[test]
fn shared_develop_parity_cache_and_folder_switch() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    let app = dir.path().join("app");
    fs::create_dir(&app).unwrap();
    let adj = transport_bench::base_adjustments();
    let mut resident_pid = None;
    for folder in ["local", "other"] {
        let root = dir.path().join(folder);
        fs::create_dir(&root).unwrap();
        for id in ["DSC04430", "DSC04880"] {
            fs::write(root.join(format!("{id}.ARW")), format!("{folder}-{id}")).unwrap();
        }
        fs::write(
            app.join("config.json"),
            json!({"source_root": root}).to_string(),
        )
        .unwrap();
        for id in ["DSC04430", "DSC04880", "DSC04430"] {
            let preview = app.join("coreimage/preview");
            if preview.exists() {
                fs::remove_dir_all(preview).unwrap();
            }
            let single = develop(
                &app,
                &cli,
                id,
                DevelopMode::Preview,
                Some(&adj),
                &slot,
                Duration::from_secs(5),
            )
            .unwrap();
            assert!(!single.cache_hit);
            let single_prov: Value =
                serde_json::from_str(&fs::read_to_string(&single.provenance_path).unwrap())
                    .unwrap();
            let bytes = fs::read(&single.output_path).unwrap();
            assert!(bytes.starts_with(format!("{folder}-{id}").as_bytes()));
            fs::remove_file(&single.output_path).unwrap();
            fs::remove_file(&single.provenance_path).unwrap();
            let resident = develop_with_executor(
                &app,
                &cli,
                id,
                DevelopMode::Preview,
                Some(&adj),
                &slot,
                Duration::from_secs(5),
                &owner,
            )
            .unwrap();
            let prov: Value =
                serde_json::from_str(&fs::read_to_string(&resident.provenance_path).unwrap())
                    .unwrap();
            assert_eq!(
                single_prov, prov,
                "same key set and values before provenance"
            );
            let resident_bytes = fs::read(&resident.output_path).unwrap();
            assert!(resident_bytes.starts_with(format!("{folder}-{id}").as_bytes()));
            assert_eq!(bytes, resident_bytes);
            assert_eq!(single.params_hash, resident.params_hash);
            assert!(!resident.cache_hit);
            assert!(prov.get("sequence").is_none());
            let pid = owner.pid(&slot.acquire().unwrap());
            assert_eq!(*resident_pid.get_or_insert(pid), pid);
            assert!(
                develop_with_executor(
                    &app,
                    &cli,
                    id,
                    DevelopMode::Preview,
                    Some(&adj),
                    &slot,
                    Duration::from_secs(5),
                    &owner
                )
                .unwrap()
                .cache_hit
            );
        }
    }
    // Replacing the binary must reject even a completed-JPEG hit before reading stale memos.
    let original = fs::read(&cli).unwrap();
    fs::write(&cli, [original.as_slice(), b"\n# new"].concat()).unwrap();
    assert_eq!(
        develop_with_executor(
            &app,
            &cli,
            "DSC04430",
            DevelopMode::Preview,
            Some(&adj),
            &slot,
            Duration::from_secs(5),
            &owner
        )
        .unwrap_err()
        .code,
        "SIDECAR_IDENTITY_CHANGED"
    );
}

#[test]
fn common_output_validation_evicts_resident_without_replay() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    let input = dir.path().join("DSC00001.ARW");
    fs::write(&input, b"raw").unwrap();
    // Inject the fixture action in adjustments (top-level requests remain shared).
    let req = request(dir.path(), "badoutput");
    let response = run(&owner, &cli, &req, &slot).unwrap();
    assert_eq!(response["width"], 0);
    // Exercise the seam's common path separately using a fake executor that delegates and
    // corrupts output dimensions, just as a semantically malformed CLI response would.
    struct Bad<'a>(&'a ResidentOwner<'a>);
    impl DevelopExecutor for Bad<'_> {
        fn execute(
            &self,
            sidecar: &Path,
            req: &Value,
            timeout: Duration,
            guard: &SlotGuard<'_>,
        ) -> Result<Value, CoreImageError> {
            let mut req = req.clone();
            req["test_action"] = json!("badoutput");
            self.0.request(sidecar, &req, timeout, guard)
        }
        fn invalidate(&self, guard: &SlotGuard<'_>) {
            self.0.invalidate(guard);
        }
    }
    assert_eq!(
        develop_to_output_with_executor(
            &cli,
            &input,
            &dir.path().join("bad.jpg"),
            DevelopMode::Preview,
            None,
            Duration::from_secs(5),
            &slot.acquire().unwrap(),
            &Bad(&owner)
        )
        .unwrap_err()
        .code,
        "SIDECAR_BAD_RESPONSE"
    );
    assert_eq!(owner.pid(&slot.acquire().unwrap()), None);
    assert_eq!(lines(dir.path(), "calls").len(), 2);
}

#[test]
fn idle_resident_coexists_with_single_but_active_develop_is_one() {
    let dir = Dir::new();
    let cli = mock(dir.path());
    let slot = DevelopSlot::new(0);
    let owner = ResidentOwner::new(&cli, Policy::default(), &slot).unwrap();
    run(&owner, &cli, &request(dir.path(), "ok"), &slot).unwrap();
    let resident_pid = owner.pid(&slot.acquire().unwrap()).unwrap();
    let req = request(dir.path(), "block");
    thread::scope(|scope| {
        let single = scope.spawn(|| {
            SingleExecutor.execute(
                &cli,
                &req,
                Duration::from_secs(20),
                &slot.acquire().unwrap(),
            )
        });
        wait_until(|| lines(dir.path(), "calls").len() == 2);
        let pids: Vec<_> = lines(dir.path(), "starts")
            .iter()
            .map(|p| p.parse::<u32>().unwrap())
            .collect();
        assert_eq!(pids.len(), 2);
        assert!(pids.iter().all(|pid| alive(*pid)));
        assert!(alive(resident_pid));
        assert_eq!(slot.acquire().err().unwrap().code, "BUSY");
        // Default policy disables expiry: this checks idle coexistence, not busy-owner rejection.
        assert!(!owner.reap_idle());
        fs::write(dir.path().join("release"), b"go").unwrap();
        single.join().unwrap().unwrap();
    });
    assert_eq!(owner.pid(&slot.acquire().unwrap()), Some(resident_pid));
    assert_eq!(slot.pending(), 0);
}

#[test]
fn canonical_path_change_is_rejected_even_for_identical_bytes() {
    let dir = Dir::new();
    let original = mock(dir.path());
    let slot = DevelopSlot::new(2);
    let link = dir.path().join("linked-cli");
    std::os::unix::fs::symlink(&original, &link).unwrap();
    let owner = ResidentOwner::new(&link, Policy::default(), &slot).unwrap();
    run(&owner, &link, &request(dir.path(), "ok"), &slot).unwrap();
    let pid = owner.pid(&slot.acquire().unwrap()).unwrap();
    let copied = dir.path().join("copy-cli");
    fs::copy(original, &copied).unwrap();
    fs::remove_file(&link).unwrap();
    std::os::unix::fs::symlink(copied, &link).unwrap();
    assert_eq!(
        run(&owner, &link, &request(dir.path(), "ok"), &slot)
            .unwrap_err()
            .code,
        "SIDECAR_IDENTITY_CHANGED"
    );
    assert!(!alive(pid));
    assert_eq!(lines(dir.path(), "calls").len(), 1);
}
