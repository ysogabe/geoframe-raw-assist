//! PR-A stateless owner, available only to Rust tests until PR-B's preview feature.
//! Lock order: caller's DevelopSlot -> state. No owner operation acquires a slot.
use super::*;
use std::os::unix::fs::MetadataExt;
use std::sync::TryLockError;

#[derive(Debug, PartialEq, Eq)]
struct Identity {
    canonical: PathBuf,
    stamp: ConfigStamp,
    dev: u64,
    changed: (i64, i64),
}

impl Identity {
    fn read(path: &Path) -> Result<Self, CoreImageError> {
        let canonical = path.canonicalize().map_err(identity_io)?;
        let metadata = fs::metadata(&canonical).map_err(identity_io)?;
        Ok(Self {
            canonical,
            stamp: ConfigStamp::of(&metadata),
            dev: metadata.dev(),
            changed: (metadata.ctime(), metadata.ctime_nsec()),
        })
    }
}

fn identity_io(_: std::io::Error) -> CoreImageError {
    CoreImageError::new(
        "SIDECAR_IDENTITY_CHANGED",
        "CLI の同一性を確認できません。アプリを再起動してください",
    )
}

fn identity_changed() -> CoreImageError {
    CoreImageError::new(
        "SIDECAR_IDENTITY_CHANGED",
        "CLI が差し替えられました。アプリを再起動してください",
    )
}

/// Experiment knobs, not tuned production thresholds. None disables each recycle condition.
#[derive(Clone, Copy, Default)]
pub(super) struct Policy {
    pub(super) idle_ttl: Option<Duration>,
    pub(super) max_requests: Option<u64>,
}

#[derive(Default)]
struct State {
    session: Option<batch::BatchSession>,
    last_used: Option<Instant>,
    requests: u64,
    /// Sticky across eviction/close/poison; memoized CLI version/hash cannot follow replacement.
    identity_invalid: bool,
}

impl State {
    fn evict(&mut self) {
        drop(self.session.take()); // EOF, bounded grace, kill/wait fallback (BatchSession::Drop).
        self.last_used = None;
        self.requests = 0;
    }
}

pub(super) struct ResidentOwner {
    identity: Identity,
    state: Mutex<State>,
    policy: Policy,
}

impl ResidentOwner {
    pub(super) fn new(sidecar: &Path, policy: Policy) -> Result<Self, CoreImageError> {
        let identity = Identity::read(sidecar)?;
        // Once per owner: reject a new owner over an already memoized old binary, too.
        // Do not silently reset process-wide version/hash memos on close/recreation.
        let mut file = fs::File::open(&identity.canonical).map_err(identity_io)?;
        let mut hash = Sha256::new();
        std::io::copy(&mut file, &mut hash).map_err(identity_io)?;
        let actual: String = hash.finalize().iter().map(|b| format!("{b:02x}")).collect();
        if actual != sidecar_sha256(sidecar)? || Identity::read(sidecar)? != identity {
            return Err(identity_changed());
        }
        Ok(Self {
            identity,
            state: Mutex::new(State::default()),
            policy,
        })
    }

    fn lock(&self, _slot: &SlotGuard<'_>) -> MutexGuard<'_, State> {
        match self.state.lock() {
            Ok(state) => state,
            Err(poisoned) => {
                let mut state = poisoned.into_inner();
                state.evict();
                self.state.clear_poison();
                state
            }
        }
    }

    fn check_identity(&self, sidecar: &Path, state: &mut State) -> Result<(), CoreImageError> {
        if state.identity_invalid || Identity::read(sidecar).as_ref() != Ok(&self.identity) {
            state.identity_invalid = true;
            state.evict();
            return Err(identity_changed());
        }
        Ok(())
    }

    fn expired(&self, state: &State, now: Instant) -> bool {
        self.policy.idle_ttl.is_some_and(|ttl| {
            state
                .last_used
                .is_some_and(|used| now.saturating_duration_since(used) >= ttl)
        }) || self
            .policy
            .max_requests
            .is_some_and(|limit| state.requests >= limit)
    }

    /// Exactly one send attempt. Only a child proven dead *before* sending is replaced inline.
    pub(super) fn request(
        &self,
        sidecar: &Path,
        request: &Value,
        timeout: Duration,
        slot: &SlotGuard<'_>,
    ) -> Result<Value, CoreImageError> {
        let Some(object) = request.as_object() else {
            return Err(CoreImageError::new(
                "INVALID_REQUEST",
                "stateless request must be an object",
            ));
        };
        if object.contains_key("op")
            || object.contains_key("handle")
            || request["mode"] != "preview"
            || request["format"] != "jpeg"
            || request["max_edge"] != PREVIEW_MAX_EDGE
        {
            return Err(CoreImageError::new(
                "INVALID_REQUEST",
                "resident accepts only stateless 2400 JPEG preview requests",
            ));
        }
        let mut state = self.lock(slot);
        self.check_identity(sidecar, &mut state)?;
        if self.expired(&state, Instant::now()) {
            state.evict();
        }
        if let Some(session) = state.session.as_mut() {
            match session.is_alive() {
                Ok(true) => {}
                Ok(false) => state.evict(),
                Err(error) => {
                    state.evict();
                    return Err(error);
                }
            }
        }
        if state.session.is_none() {
            state.session = Some(batch::BatchSession::start(
                &self.identity.canonical,
                timeout,
            )?);
        }
        // BatchSession's timeout is per session. A caller may change its budget between requests.
        let session = state.session.as_mut().unwrap();
        session.set_timeout(timeout);
        let result = session.request(request);
        state.requests += 1;
        state.last_used = Some(Instant::now());
        // Even if write_all/flush failed before sent was incremented, the child might have run
        // part/all of the request. Evict and return once; the next independent call starts lazily.
        if !state.session.as_ref().unwrap().is_usable()
            || !state.session.as_ref().unwrap().reply_valid()
        {
            state.evict();
        }
        self.check_identity(sidecar, &mut state)?;
        result.map(|mut response| {
            response.as_object_mut().unwrap().remove("sequence");
            response
        })
    }

    /// Nonblocking housekeeping: a busy owner is never interrupted and no slot is acquired.
    pub(super) fn reap_idle(&self) -> bool {
        let mut state = match self.state.try_lock() {
            Ok(state) => state,
            Err(TryLockError::WouldBlock) => return false,
            Err(TryLockError::Poisoned(poisoned)) => {
                let mut state = poisoned.into_inner();
                state.evict();
                self.state.clear_poison();
                return true;
            }
        };
        if state.session.is_some() && self.expired(&state, Instant::now()) {
            state.evict();
            true
        } else {
            false
        }
    }

    /// Bounded explicit close. A concurrent request returns BUSY instead of blocking/being killed.
    pub(super) fn close(&self) -> Result<(), CoreImageError> {
        let mut state = match self.state.try_lock() {
            Ok(state) => state,
            Err(TryLockError::WouldBlock) => {
                return Err(CoreImageError::new("BUSY", "resident request is active"))
            }
            Err(TryLockError::Poisoned(poisoned)) => {
                self.state.clear_poison();
                poisoned.into_inner()
            }
        };
        state.evict();
        Ok(())
    }

    /// Request-boundary observation only. This is not a peak-RSS measurement.
    pub(super) fn pid(&self, slot: &SlotGuard<'_>) -> Option<u32> {
        self.lock(slot)
            .session
            .as_ref()
            .and_then(batch::BatchSession::pid)
    }
}

impl Drop for ResidentOwner {
    fn drop(&mut self) {
        // Exclusive ownership: no concurrent request can still borrow this owner.
        self.state
            .get_mut()
            .unwrap_or_else(|p| p.into_inner())
            .evict();
    }
}

impl DevelopExecutor for ResidentOwner {
    fn needs_identity_guard(&self) -> bool {
        true
    }
    fn validate_identity(
        &self,
        sidecar: &Path,
        slot: &SlotGuard<'_>,
    ) -> Result<(), CoreImageError> {
        self.check_identity(sidecar, &mut self.lock(slot))
    }
    fn execute(
        &self,
        sidecar: &Path,
        request: &Value,
        timeout: Duration,
        slot: &SlotGuard<'_>,
    ) -> Result<Value, CoreImageError> {
        self.request(sidecar, request, timeout, slot)
    }
    fn invalidate(&self, slot: &SlotGuard<'_>) {
        self.lock(slot).evict();
    }
}

#[cfg(test)]
mod tests;

mod harness;
