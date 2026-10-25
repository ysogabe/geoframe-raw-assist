//! QA-only wiring of the resident owner (#141 B1 PR-B). Compiled for Rust tests and the
//! `resident-preview-qa` feature only; nothing here is reachable from an ordinary product build.
//!
//! * Only preview develops go through the resident child. Full (and every other caller of the
//!   shared slot) keeps the single-shot CLI. No runtime/env switch exists.
//! * The owner borrows the app-wide `DEVELOP_SLOT`; request/cache key/output/EXIF/prune stay in
//!   the shared `develop_with_executor`.
//! * Provenance written by a resident execution carries `resident_qa` evidence (executor and the
//!   pid that was sent the request). Single-produced entries and old entries have no such key, and
//!   a cache hit returns whatever the producing run wrote; it is never rewritten.
//! * `idle_ttl`, `max_requests` and the reaper interval below are uncalibrated experiment knobs
//!   for QA, not production thresholds.
use super::*;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::Arc;
use std::thread::JoinHandle;

/// The only application identifier under which the feature path may be enabled.
pub(crate) const QA_IDENTIFIER: &str = "com.geoframe.rawassist.qaroadmap";
/// Provenance key and values for resident executions.
pub(crate) const EVIDENCE_KEY: &str = "resident_qa";
pub(crate) const EVIDENCE_EXECUTOR: &str = "resident_batch";
const EVIDENCE_SCHEMA: u32 = 1;

/// Uncalibrated QA knobs (not tuned, not an acceptance threshold).
pub(crate) const QA_IDLE_TTL: Duration = Duration::from_secs(90);
pub(crate) const QA_MAX_REQUESTS: u64 = 64;
pub(crate) const REAPER_INTERVAL: Duration = Duration::from_secs(5);
/// Upper bound on how long exit cleanup polls a busy owner before relying on process exit (EOF).
#[cfg(feature = "resident-preview-qa")]
pub(crate) const SHUTDOWN_WAIT: Duration = Duration::from_secs(3);
const SHUTDOWN_POLL: Duration = Duration::from_millis(25);

/// Refuses every identifier except the dedicated QA one.
pub(crate) fn check_identifier(actual: &str) -> Result<(), CoreImageError> {
    if actual == QA_IDENTIFIER {
        return Ok(());
    }
    Err(CoreImageError::new(
        "RESIDENT_QA_WRONG_APP_ID",
        format!(
            "resident-preview-qa は専用 QA ID {QA_IDENTIFIER} のビルドでのみ使えます（現在: {actual}）。\
             通常 ID では feature なしでビルドしてください"
        ),
    ))
}

/// Resident executor with QA evidence. The plain `ResidentOwner` executor stays free of it so the
/// PR-A Single/Resident provenance parity is untouched.
struct QaExecutor<'a, 'slot>(&'a ResidentOwner<'slot>);

impl DevelopExecutor for QaExecutor<'_, '_> {
    fn needs_identity_guard(&self) -> bool {
        true
    }
    fn validate_identity(
        &self,
        sidecar: &Path,
        slot: &SlotGuard<'_>,
    ) -> Result<(), CoreImageError> {
        DevelopExecutor::validate_identity(self.0, sidecar, slot)
    }
    fn execute(
        &self,
        sidecar: &Path,
        request: &Value,
        timeout: Duration,
        slot: &SlotGuard<'_>,
    ) -> Result<Value, CoreImageError> {
        let (mut response, pid) = self.0.request_observed(sidecar, request, timeout, slot)?;
        // A fact about this execution (the pid read under the owner's lock), not a liveness claim.
        // The cache key was fixed from the request before this point and never sees it.
        response[EVIDENCE_KEY] = json!({
            "schema": EVIDENCE_SCHEMA,
            "executor": EVIDENCE_EXECUTOR,
            "owner_pid": pid,
        });
        Ok(response)
    }
    fn invalidate(&self, slot: &SlotGuard<'_>) {
        DevelopExecutor::invalidate(self.0, slot)
    }
}

/// Owner plus the sidecar path it was created for (fixed for the app lifetime).
struct Pinned<'slot> {
    sidecar: PathBuf,
    owner: ResidentOwner<'slot>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ShutdownOutcome {
    /// No preview ever ran, so there was nothing to close.
    NotStarted,
    /// The owner was closed (EOF -> bounded grace -> kill/wait) with no request in flight.
    Closed,
    /// A request still held the owner after the wait. It is never killed here; new requests are
    /// refused, and the child sees stdin EOF only when the process itself exits.
    StillBusy,
}

/// Lazily created, app-lifetime resident preview executor bound to one develop slot.
pub(crate) struct ResidentPreview<'slot> {
    slot: &'slot DevelopSlot,
    policy: Policy,
    pinned: Mutex<Option<Arc<Pinned<'slot>>>>,
    stopped: AtomicBool,
}

impl<'slot> ResidentPreview<'slot> {
    pub(in crate::coreimage) fn new(slot: &'slot DevelopSlot, policy: Policy) -> Self {
        Self {
            slot,
            policy,
            pinned: Mutex::new(None),
            stopped: AtomicBool::new(false),
        }
    }

    fn pinned(&self, sidecar: &Path) -> Result<Arc<Pinned<'slot>>, CoreImageError> {
        let mut pinned = self
            .pinned
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        // Under the same mutex shutdown sets `stopped`, so no owner appears after shutdown began.
        if self.stopped.load(Ordering::Acquire) {
            return Err(CoreImageError::new(
                "RESIDENT_CLOSED",
                "resident preview is shut down",
            ));
        }
        if let Some(existing) = pinned.as_ref() {
            // One sidecar for the whole app lifetime; a different path is a replaced binary.
            if existing.sidecar != sidecar {
                return Err(identity_changed());
            }
            return Ok(Arc::clone(existing));
        }
        // No silent re-creation: `ResidentOwner::new` also checks the real hash against the memo.
        let created = Arc::new(Pinned {
            sidecar: sidecar.to_path_buf(),
            owner: ResidentOwner::new(sidecar, self.policy, self.slot)?,
        });
        *pinned = Some(Arc::clone(&created));
        Ok(created)
    }

    /// Preview goes through the resident child; every other mode is the unchanged single-shot path.
    /// Full never reaches the resident executor, so its cache-bypass semantics are not involved.
    pub(crate) fn develop(
        &self,
        app_data_dir: &Path,
        sidecar: &Path,
        capture_id: &str,
        mode: DevelopMode,
        adjustments: Option<&Value>,
        timeout: Duration,
    ) -> Result<CoreImagePreviewResult, CoreImageError> {
        if mode != DevelopMode::Preview {
            return develop(
                app_data_dir,
                sidecar,
                capture_id,
                mode,
                adjustments,
                self.slot,
                timeout,
            );
        }
        let pinned = self.pinned(sidecar)?;
        develop_with_executor(
            app_data_dir,
            &pinned.sidecar,
            capture_id,
            mode,
            adjustments,
            self.slot,
            timeout,
            &QaExecutor(&pinned.owner),
        )
    }

    /// Nonblocking: never takes the slot and never interrupts a request holding the owner.
    pub(crate) fn reap_idle(&self) -> bool {
        let pinned = self
            .pinned
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        pinned.is_some_and(|pinned| pinned.owner.reap_idle())
    }

    /// Final close. Polls a busy owner for at most `wait` (never kills active work, never takes
    /// the slot), then reports `StillBusy`. After this call no new child can be started.
    pub(crate) fn shutdown(&self, wait: Duration) -> ShutdownOutcome {
        let pinned = {
            let pinned = self
                .pinned
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            self.stopped.store(true, Ordering::Release);
            pinned.clone()
        };
        let Some(pinned) = pinned else {
            return ShutdownOutcome::NotStarted;
        };
        let deadline = Instant::now() + wait;
        loop {
            match pinned.owner.shutdown() {
                Ok(()) => return ShutdownOutcome::Closed,
                Err(_) if Instant::now() >= deadline => return ShutdownOutcome::StillBusy,
                Err(_) => thread::sleep(SHUTDOWN_POLL),
            }
        }
    }
}

/// Background idle reaper. It only calls `reap_idle` (try_lock), so it never waits for the slot
/// or the owner and can always be stopped promptly.
pub(crate) struct Reaper {
    stop: Option<mpsc::Sender<()>>,
    join: Option<JoinHandle<()>>,
}

impl Reaper {
    pub(crate) fn spawn(preview: Arc<ResidentPreview<'static>>, interval: Duration) -> Self {
        let (stop, ticks) = mpsc::channel();
        let join = thread::spawn(move || {
            while let Err(RecvTimeoutError::Timeout) = ticks.recv_timeout(interval) {
                if preview.reap_idle() {
                    eprintln!("[resident-qa] idle resident child closed by reaper");
                }
            }
        });
        Self {
            stop: Some(stop),
            join: Some(join),
        }
    }

    pub(crate) fn stop(&mut self) {
        drop(self.stop.take()); // Disconnect (or send) wakes the loop immediately.
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
    }
}

impl Drop for Reaper {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Tauri managed state: resident preview plus its reaper, tied to the application lifetime.
pub(crate) struct QaApp {
    preview: Arc<ResidentPreview<'static>>,
    reaper: Mutex<Reaper>,
}

impl QaApp {
    pub(in crate::coreimage) fn start(
        slot: &'static DevelopSlot,
        policy: Policy,
        interval: Duration,
    ) -> Self {
        let preview = Arc::new(ResidentPreview::new(slot, policy));
        let reaper = Mutex::new(Reaper::spawn(Arc::clone(&preview), interval));
        Self { preview, reaper }
    }

    /// App-wide slot and the documented (uncalibrated) QA knobs.
    pub(crate) fn start_default() -> Self {
        Self::start(
            &DEVELOP_SLOT,
            Policy {
                idle_ttl: Some(QA_IDLE_TTL),
                max_requests: Some(QA_MAX_REQUESTS),
            },
            REAPER_INTERVAL,
        )
    }

    pub(crate) fn preview(&self) -> &ResidentPreview<'static> {
        &self.preview
    }

    /// Idempotent. Stops the reaper first, then closes the owner (bounded, never kills active work).
    pub(crate) fn shutdown(&self, wait: Duration) -> ShutdownOutcome {
        self.reaper
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .stop();
        self.preview.shutdown(wait)
    }
}

/// `setup` hook: refuse any non-QA identifier, then manage the app-lifetime state.
#[cfg(feature = "resident-preview-qa")]
pub(crate) fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    check_identifier(&app.config().identifier).map_err(|error| error.message)?;
    app.manage(QaApp::start_default());
    Ok(())
}

/// `RunEvent::Exit` hook. Logs the outcome without any path or capture information.
#[cfg(feature = "resident-preview-qa")]
pub(crate) fn on_exit(app: &AppHandle) {
    if let Some(state) = app.try_state::<QaApp>() {
        eprintln!(
            "[resident-qa] exit cleanup: {:?}",
            state.shutdown(SHUTDOWN_WAIT)
        );
    }
}

#[cfg(test)]
mod tests;
