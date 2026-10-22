// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! GeoFrame RAW Assist desktop shell.
//!
//! This native window hosts the existing React SPA (built to `../dist`, or
//! served by `vite` on `http://localhost:1420` in dev) and owns the
//! lifecycle of the local Python RAW-engine sidecar
//! (`local_raw/server.py`, `http://127.0.0.1:8788`) so the end user never
//! has to start it in a second terminal.
//!
//! The Python↔TypeScript HTTP API (`docs/API_CONTRACT.md`) is frozen and
//! untouched by this shell — Rust only spawns/reaps the process; all
//! request/response handling stays in `app/lib/raw-api.ts` (soon
//! `src/lib/raw-api.ts`), which already treats an unreachable engine as a
//! recoverable state via `isEndpointUnavailable`/`getHealth`. So every
//! failure path here logs to stderr and leaves the window open rather than
//! panicking.

mod adjustments;
mod coreimage;
mod presets;

use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use signal_hook::consts::TERM_SIGNALS;
use signal_hook::iterator::Signals;
use tauri::{AppHandle, Emitter, Manager, RunEvent};

/// Holds the spawned RAW-engine child process, if any. Managed as Tauri
/// app state so both the `setup` hook (spawn) and the exit handler (kill)
/// can reach it.
struct RawEngineState(Mutex<Option<Child>>);

/// Most recent lines kept in `SetupState.log`. Old lines are dropped once
/// this cap is exceeded so a slow/chatty `pip install` can't grow the
/// event payload unboundedly.
const MAX_SETUP_LOG_LINES: usize = 500;

/// Snapshot of the auto-bootstrap process that creates `.venv` and installs
/// `requirements-raw.txt` on first launch (see `run_setup_bootstrap`
/// below). Managed as Tauri app state (`Mutex<SetupState>`) and mirrored to
/// the frontend two ways: pushed via the `"raw-setup"` event on every
/// change, and pull-able at any time via the `setup_status` command so a
/// frontend that mounts after a transition already happened doesn't race
/// the event stream.
#[derive(Clone, serde::Serialize)]
struct SetupState {
    /// One of: "ready" | "creating-venv" | "installing-deps" | "succeeded" | "failed".
    stage: String,
    /// Current human-readable (Japanese) status line.
    message: String,
    /// Accumulated stdout/stderr lines from the bootstrap subprocesses, in
    /// order, capped at `MAX_SETUP_LOG_LINES` entries.
    log: Vec<String>,
}

impl SetupState {
    fn new(stage: &str, message: impl Into<String>) -> Self {
        Self {
            stage: stage.to_string(),
            message: message.into(),
            log: Vec::new(),
        }
    }
}

/// Returns the current `SetupState` so a frontend that mounts after a
/// stage transition already happened can pull it directly instead of
/// waiting for (and possibly missing) the next `"raw-setup"` event.
#[tauri::command]
fn setup_status(state: tauri::State<Mutex<SetupState>>) -> SetupState {
    let guard = match state.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    guard.clone()
}

/// The repository root: the parent directory of this `src-tauri` crate,
/// resolved at *compile time* via `CARGO_MANIFEST_DIR` — never via the
/// process's current working directory, which Tauri does not guarantee
/// matches the repo root (e.g. when launched from Finder or a bundled
/// `.app`).
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("src-tauri must live directly under the repo root")
        .to_path_buf()
}

/// Path to the Python interpreter inside the `.venv` created by
/// `npm run setup:raw` (or, on first launch, by `run_setup_bootstrap`
/// below). Shared by `resolve_python_binary` (RAW-engine launch) and the
/// `.setup()` hook's "does the venv already exist" check.
fn venv_python_path() -> PathBuf {
    repo_root().join(".venv").join("bin").join("python")
}

/// The single source of truth for the Python packages the RAW engine needs,
/// installed by `npm run setup:raw` and by `run_pip_install` below.
fn requirements_path() -> PathBuf {
    repo_root().join("requirements-raw.txt")
}

/// Records which `requirements-raw.txt` the current `.venv` was last
/// *successfully* installed from — see `dependencies_up_to_date`.
///
/// Deliberately lives inside `.venv` rather than next to it: the marker
/// describes the state of that one venv, so deleting the venv must also
/// discard the marker (otherwise a `rm -rf .venv` would leave a stale
/// "already installed" claim behind). It is also invisible to git twice
/// over — `/.venv/` is in the repo's `.gitignore`, and `python -m venv`
/// writes a catch-all `.gitignore` inside the venv itself.
fn requirements_marker_path() -> PathBuf {
    repo_root().join(".venv").join(".requirements-installed")
}

/// Whether the existing `.venv` was already built from the current
/// `requirements-raw.txt`.
///
/// The marker stores the requirements file verbatim (it is a couple of
/// hundred bytes of committed plaintext), so this is an exact comparison
/// rather than a digest: no collisions to reason about, no hashing code to
/// maintain, and `cat .venv/.requirements-installed` shows a human exactly
/// which dependency set the venv holds.
///
/// Fails toward re-installing: a marker that is missing (first launch after
/// this check shipped, or one deleted by hand), unreadable, or truncated is
/// treated the same as a mismatch, because none of those can *prove* the
/// venv is current. The one case that returns `true` without a match is an
/// unreadable `requirements-raw.txt` — there is then nothing to install
/// from, so re-running pip could only fail, and the engine should start with
/// whatever the venv already has, exactly as it did before this check
/// existed.
fn dependencies_up_to_date() -> bool {
    let requirements = match fs::read_to_string(requirements_path()) {
        Ok(requirements) => requirements,
        Err(err) => {
            eprintln!(
                "[raw-setup] could not read {} ({err}); skipping the dependency check",
                requirements_path().display()
            );
            return true;
        }
    };

    matches!(fs::read_to_string(requirements_marker_path()), Ok(recorded) if recorded == requirements)
}

/// Resolves the Python interpreter used to run the RAW engine.
///
/// Priority:
/// 1. `RAW_ASSIST_PYTHON` env var, if set (escape hatch for custom
///    interpreters/venvs).
/// 2. `<repoRoot>/.venv/bin/python`, the venv created by `npm run setup:raw`
///    (or by the first-launch auto-bootstrap thread, see
///    `run_setup_bootstrap`).
///
/// NOTE: production bundling of a self-contained Python runtime (so
/// end users don't need `.venv` at all) is out of scope for this shell.
/// The env-var override above is the escape hatch for now; a real fix
/// would resolve a bundled interpreter from Tauri's resource directory
/// here instead of falling through to `.venv`.
fn resolve_python_binary() -> PathBuf {
    if let Ok(path) = env::var("RAW_ASSIST_PYTHON") {
        if !path.trim().is_empty() {
            return PathBuf::from(path);
        }
    }
    venv_python_path()
}

/// Opens (creating the directory and truncating any previous file)
/// `<app_data_dir>/logs/raw-engine.log` for the RAW-engine child's
/// stdout/stderr.
///
/// Returns `None` (after warning on stderr) if the app-data directory
/// can't be resolved, the `logs` directory can't be created, or the file
/// can't be opened -- callers must treat that as "fall back to inheriting
/// the parent's stdio", not as a reason to abort the launch. One truncate
/// per process launch is deliberate (`spawn_raw_engine` normally runs
/// once per app run): this is not meant to be an append-forever log.
fn open_raw_engine_log(app_handle: &AppHandle) -> Option<fs::File> {
    let app_data_dir = match app_handle.path().app_data_dir() {
        Ok(dir) => dir,
        Err(err) => {
            eprintln!("[raw-engine] failed to resolve app data dir for logging: {err}");
            return None;
        }
    };

    let log_dir = app_data_dir.join("logs");
    if let Err(err) = fs::create_dir_all(&log_dir) {
        eprintln!("[raw-engine] failed to create log directory {}: {err}", log_dir.display());
        return None;
    }

    let log_path = log_dir.join("raw-engine.log");
    let mut options = fs::OpenOptions::new();
    options.create(true).truncate(true).write(true);
    // Match drive_upload.py's 0600 convention for this same app-data
    // directory (drive_auth.json) rather than the OpenOptions default of
    // 0666 & ~umask: engine output can carry request paths and, per
    // drive_upload.py's own HttpError handling notes, conceivably other
    // diagnostic detail, so it should not default to world-readable.
    //
    // `mode()` on `create(true)` (not `create_new`) is only honored by
    // open(2) when the file doesn't already exist -- it's silently a
    // no-op against a file some earlier run already created with looser
    // permissions. `set_permissions` below covers that case explicitly
    // instead of relying on every past run of this binary having gotten
    // the mode right.
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    match options.open(&log_path) {
        Ok(file) => {
            #[cfg(unix)]
            if let Err(err) = file.set_permissions(std::os::unix::fs::PermissionsExt::from_mode(0o600)) {
                eprintln!("[raw-engine] failed to set log file permissions on {}: {err}", log_path.display());
            }
            eprintln!("[raw-engine] logging to {}", log_path.display());
            Some(file)
        }
        Err(err) => {
            eprintln!("[raw-engine] failed to open log file {}: {err}", log_path.display());
            None
        }
    }
}

/// Spawns `python -m local_raw.server` with `current_dir` set to the repo
/// root (server.py itself also resolves `PROJECT_ROOT` independently of
/// cwd, but we match it anyway for predictable relative-path behavior).
///
/// stdout/stderr are redirected to `<app_data_dir>/logs/raw-engine.log`
/// (see `open_raw_engine_log`) so the engine's output is somewhere durable
/// instead of only ever reaching a parent stdio that may not exist (a
/// bundled `.app` has no console) or that dumps it into a Terminal window
/// the user did not ask for (a double-clicked unbundled binary). If the
/// log file can't be opened, this falls back to inheriting the parent's
/// stdio (the pre-existing behavior) rather than failing the launch.
///
/// Never panics: a missing venv or a spawn failure is logged to stderr and
/// the shell keeps going with `None`, matching the frontend's existing
/// "engine unreachable" health-check gate.
fn spawn_raw_engine(app_handle: &AppHandle) -> Option<Child> {
    let python = resolve_python_binary();

    if !python.is_file() {
        eprintln!(
            "[raw-engine] python interpreter not found at {} \
             (set RAW_ASSIST_PYTHON to override, or run `npm run setup:raw` \
             to create .venv). The RAW engine will not start automatically; \
             the UI will report it unreachable until it is started manually.",
            python.display()
        );
        return None;
    }

    let mut command = Command::new(&python);
    command
        .args(["-m", "local_raw.server"])
        .current_dir(repo_root())
        .stdin(Stdio::null())
        // stdout/stderr now land in a real file instead of a tty, which
        // switches CPython from line-buffered to ~8KiB block-buffered.
        // kill_raw_engine's Child::kill() is SIGKILL on Unix -- no atexit,
        // no flush -- so without this, exactly the output most useful for
        // diagnosing a bad shutdown (server.py's log_message/_fail, which
        // print() without flush=True) is the output most likely to be lost.
        .env("PYTHONUNBUFFERED", "1");

    if cfg!(debug_assertions) {
        // `npm run tauri:dev` is always launched from a terminal by a
        // developer who wants to see the engine's output live -- keep the
        // pre-existing inherit behavior there instead of sending it to a
        // log file the developer would otherwise have to go find. Only
        // release builds (the ones actually double-clicked as a `.app` or
        // run headless) redirect to `raw-engine.log`.
        command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
    } else {
        match open_raw_engine_log(app_handle).and_then(|file| match file.try_clone() {
            Ok(cloned) => Some((file, cloned)),
            Err(err) => {
                eprintln!("[raw-engine] failed to clone log file handle: {err}");
                None
            }
        }) {
            Some((stdout_file, stderr_file)) => {
                command.stdout(Stdio::from(stdout_file)).stderr(Stdio::from(stderr_file));
            }
            None => {
                command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
            }
        }
    }

    match command.spawn() {
        Ok(child) => {
            eprintln!("[raw-engine] started (pid {}) via {}", child.id(), python.display());
            Some(child)
        }
        Err(err) => {
            eprintln!("[raw-engine] failed to spawn {}: {err}", python.display());
            None
        }
    }
}

/// Kills the managed child process if it's still running. Idempotent: safe
/// to call more than once (e.g. from both `ExitRequested` and `Exit`).
fn kill_raw_engine(state: &RawEngineState) {
    let mut guard = match state.0.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };

    let Some(mut child) = guard.take() else {
        return;
    };

    match child.try_wait() {
        Ok(Some(_)) => {
            // Already exited on its own; nothing to do.
        }
        Ok(None) => {
            if let Err(err) = child.kill() {
                eprintln!("[raw-engine] failed to kill child process: {err}");
            }
            let _ = child.wait();
        }
        Err(err) => {
            eprintln!("[raw-engine] failed to poll child process status: {err}");
        }
    }
}

/// Replaces the managed `SetupState` wholesale — used for stage
/// transitions (as opposed to `append_setup_log`'s incremental log-only
/// updates) — updating the `Mutex` and then emitting `"raw-setup"` with
/// the new state so any frontend already listening gets a live update.
fn update_setup_state(app_handle: &AppHandle, next: SetupState) {
    {
        let state = app_handle.state::<Mutex<SetupState>>();
        let mut guard = match state.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        *guard = next.clone();
    }
    if let Err(err) = app_handle.emit("raw-setup", next) {
        eprintln!("[raw-setup] failed to emit raw-setup event: {err}");
    }
}

/// Like `update_setup_state`, but carries the log accumulated so far into
/// the next state instead of starting an empty one. Used when a transition
/// has to stay diagnosable even though the new stage is not one the
/// frontend renders a log for — specifically the deliberately non-blocking
/// "the dependency refresh failed" state in `run_dependency_refresh`, whose
/// pip output would otherwise be dropped from `setup_status`.
fn update_setup_state_keeping_log(app_handle: &AppHandle, stage: &str, message: impl Into<String>) {
    let log = {
        let state = app_handle.state::<Mutex<SetupState>>();
        let guard = match state.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard.log.clone()
    };
    update_setup_state(
        app_handle,
        SetupState {
            stage: stage.to_string(),
            message: message.into(),
            log,
        },
    );
}

/// Appends one line to the managed `SetupState.log` (capping it at
/// `MAX_SETUP_LOG_LINES` entries, dropping the oldest first), keeping the
/// current stage/message untouched, then re-emits `"raw-setup"` with the
/// updated state. Also echoed to stderr so `pip install` progress is
/// visible in the terminal for anyone running `npm run tauri:dev`.
fn append_setup_log(app_handle: &AppHandle, line: String) {
    eprintln!("[raw-setup] {line}");
    let next = {
        let state = app_handle.state::<Mutex<SetupState>>();
        let mut guard = match state.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard.log.push(line);
        let overflow = guard.log.len().saturating_sub(MAX_SETUP_LOG_LINES);
        if overflow > 0 {
            guard.log.drain(0..overflow);
        }
        guard.clone()
    };
    if let Err(err) = app_handle.emit("raw-setup", next) {
        eprintln!("[raw-setup] failed to emit raw-setup event: {err}");
    }
}

/// Joins the last `lines` entries of `log` into a single string, for
/// embedding a short tail of subprocess output into a `"failed"` stage's
/// `message` (the full log already lives in `SetupState.log`; this just
/// gives the headline message enough context to be useful on its own).
fn log_tail(log: &[String], lines: usize) -> String {
    let start = log.len().saturating_sub(lines);
    log[start..].join("\n")
}

/// Candidate `python3` interpreters to try, in priority order, when
/// bootstrapping `.venv` from scratch:
/// 1. `RAW_ASSIST_PYTHON3` env var, if set (escape hatch, mirrors
///    `RAW_ASSIST_PYTHON` for the venv-already-exists path).
/// 2. Plain `python3` resolved via `PATH` at spawn time — works when
///    launched from a terminal or `tauri dev`.
/// 3. Fixed fallback locations, for GUI launches (double-clicking the
///    `.app` from Finder) which inherit launchd's minimal PATH
///    (`/usr/bin:/bin:/usr/sbin:/sbin`) and therefore can't see a
///    Homebrew-installed `python3` via `PATH` at all.
///
/// The `PATH`-based `python3` entry is included unconditionally (no
/// pre-existence check) — the caller tries spawning it and falls through
/// to the next candidate only on a spawn failure.
fn python3_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    if let Ok(path) = env::var("RAW_ASSIST_PYTHON3") {
        if !path.trim().is_empty() {
            candidates.push(PathBuf::from(path));
        }
    }

    candidates.push(PathBuf::from("python3"));

    for fallback in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"] {
        let fallback = Path::new(fallback);
        if fallback.is_file() {
            candidates.push(fallback.to_path_buf());
        }
    }

    candidates
}

/// Spawns `command` with stdout/stderr piped (stdin closed, matching
/// `spawn_raw_engine`'s pattern), ready for `stream_setup_output`.
fn spawn_setup_step(command: &mut Command) -> std::io::Result<Child> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
}

/// Streams `child`'s stdout and stderr into `SetupState.log` line by line
/// as it arrives — one reader thread per pipe, both funneling through
/// `append_setup_log` — then waits for the process to exit. Blocks the
/// calling thread until both pipes hit EOF and the process has exited.
fn stream_setup_output(app_handle: &AppHandle, mut child: Child) -> std::io::Result<std::process::ExitStatus> {
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    let stdout_reader = stdout.map(|pipe| {
        let app_handle = app_handle.clone();
        thread::spawn(move || {
            for line in BufReader::new(pipe).lines().map_while(Result::ok) {
                append_setup_log(&app_handle, line);
            }
        })
    });
    let stderr_reader = stderr.map(|pipe| {
        let app_handle = app_handle.clone();
        thread::spawn(move || {
            for line in BufReader::new(pipe).lines().map_while(Result::ok) {
                append_setup_log(&app_handle, line);
            }
        })
    });

    if let Some(handle) = stdout_reader {
        let _ = handle.join();
    }
    if let Some(handle) = stderr_reader {
        let _ = handle.join();
    }

    child.wait()
}

/// Creates `.venv` and installs `requirements-raw.txt` — the same two
/// steps `npm run setup:raw` performs — then spawns the RAW engine once
/// dependencies are installed. Stage/log progress is published via the
/// managed `SetupState` (`update_setup_state`/`append_setup_log`), which
/// the frontend reads through the `"raw-setup"` event and `setup_status`
/// command.
///
/// # Why this exists
///
/// `spawn_raw_engine` only finds an interpreter once `.venv/bin/python`
/// exists, which today only happens after a human runs
/// `npm run setup:raw` in a terminal. On a fresh checkout — or a
/// double-clicked `.app` bundle where nobody ever opened a terminal at
/// all — that never happens, so the window opened with the engine
/// permanently "offline" and no way to fix it short of reading this file.
/// This function makes the app self-bootstrapping: first launch pays the
/// one-time cost of `pip install` (network access + a couple of minutes),
/// every launch after that finds `.venv/bin/python` already in place and
/// the `.setup()` hook below skips straight to `spawn_raw_engine`, exactly
/// like today — unless `requirements-raw.txt` has changed since, in which
/// case `run_dependency_refresh` installs just the difference.
///
/// Must run off the main thread: `.setup()` has to return promptly so the
/// window can open, and `pip install` is a multi-minute network round
/// trip that would otherwise block it.
fn run_setup_bootstrap(app_handle: AppHandle) {
    let venv_dir = repo_root().join(".venv");

    update_setup_state(&app_handle, SetupState::new("creating-venv", "Python仮想環境を作成しています…"));

    let mut venv_child = None;
    let mut spawn_errors = Vec::new();
    for candidate in python3_candidates() {
        let mut command = Command::new(&candidate);
        command.args(["-m", "venv"]).arg(&venv_dir).current_dir(repo_root());
        match spawn_setup_step(&mut command) {
            Ok(child) => {
                venv_child = Some(child);
                break;
            }
            Err(err) => spawn_errors.push(format!("{}: {err}", candidate.display())),
        }
    }

    let Some(venv_child) = venv_child else {
        eprintln!("[raw-setup] no usable python3 found: {}", spawn_errors.join("; "));
        update_setup_state(
            &app_handle,
            SetupState::new(
                "failed",
                "python3 が見つかりませんでした。Homebrew などで Python 3 をインストールしてください。",
            ),
        );
        return;
    };

    let status = match stream_setup_output(&app_handle, venv_child) {
        Ok(status) => status,
        Err(err) => {
            eprintln!("[raw-setup] failed to wait for venv creation: {err}");
            update_setup_state(
                &app_handle,
                SetupState::new("failed", format!("仮想環境の作成中にエラーが発生しました: {err}")),
            );
            return;
        }
    };

    if !status.success() {
        let message = {
            let state = app_handle.state::<Mutex<SetupState>>();
            let guard = match state.lock() {
                Ok(guard) => guard,
                Err(poisoned) => poisoned.into_inner(),
            };
            format!(
                "仮想環境の作成に失敗しました (終了コード {}).\n{}",
                status.code().map(|c| c.to_string()).unwrap_or_else(|| "不明".to_string()),
                log_tail(&guard.log, 5)
            )
        };
        eprintln!("[raw-setup] venv creation failed: {status}");
        update_setup_state(&app_handle, SetupState::new("failed", message));
        return;
    }

    if !run_pip_install(&app_handle, "必要なライブラリをインストールしています（初回のみ、数分かかることがあります）…") {
        // Brand-new venv: without its dependencies the RAW engine cannot
        // start at all, so leave the failure on screen and do not spawn it
        // (unchanged behavior). `run_dependency_refresh` deliberately
        // differs — see its doc comment.
        return;
    }

    update_setup_state(&app_handle, SetupState::new("succeeded", "セットアップが完了しました。"));

    let child = spawn_raw_engine(&app_handle);
    let state = app_handle.state::<RawEngineState>();
    let mut guard = match state.0.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    *guard = child;
}

/// Runs `pip install -r requirements-raw.txt` with the venv's own
/// interpreter, streaming progress into `SetupState` under the
/// `"installing-deps"` stage, and returns whether it succeeded.
///
/// Shared by both callers — the first-launch bootstrap above and the
/// dependency refresh below — so that the marker write can only ever happen
/// in one place, immediately after pip exits 0. That ordering is the whole
/// point: writing the marker any earlier (or on a failed install) would
/// claim the venv satisfies a `requirements-raw.txt` it does not, and since
/// `dependencies_up_to_date` then reports a match, the user could never
/// recover by relaunching — the install would never be retried again.
///
/// `caller_message` is the headline shown while pip runs; the two callers
/// word it differently ("first launch" vs "app update").
///
/// # Retries
///
/// pip's most common failure here is not a broken `requirements-raw.txt` but
/// a flaky network — a DNS hiccup, a dropped TLS handshake, a PyPI mirror
/// returning 5xx mid-download — and every one of those succeeds on a second
/// try a few seconds later. So a non-zero *exit code* is retried up to
/// `PIP_INSTALL_MAX_ATTEMPTS` times (the first attempt plus two retries),
/// sleeping `PIP_INSTALL_RETRY_DELAY` between them, with the stage held at
/// `"installing-deps"` and the log carried over so the user sees one
/// continuous install rather than a flash of `"failed"`.
///
/// Two failure modes are deliberately *not* retried, because repeating them
/// only burns the user's time: failing to **spawn** the interpreter at all
/// (the venv's python is missing or not executable — no amount of waiting
/// fixes that) and an **I/O error while waiting** on the child, which is a
/// failure of the wait itself rather than a result the process produced.
/// Both still fail fast exactly as before.
///
/// # Cost of the retries, and why it is accepted
///
/// Because this function is shared, the retries apply to both callers.
/// `run_dependency_refresh`'s guarantee is unchanged — it still starts the
/// engine after a failure, so a transient failure still does not cost the
/// user the whole app. What *does* change is how long "no network at all"
/// takes to give up: up to `PIP_INSTALL_MAX_ATTEMPTS` full pip attempts plus
/// the waits between them, where it used to be one. That is the accepted
/// trade — an offline launch is slower to reach the window, in exchange for
/// a flaky-network launch usually not failing at all. Both callers already
/// run off the main thread (see their doc comments), so the blocking sleep
/// delays only this bootstrap thread and never the UI.
fn run_pip_install(app_handle: &AppHandle, caller_message: &str) -> bool {
    /// Total pip attempts before giving up: the first try plus two retries.
    const PIP_INSTALL_MAX_ATTEMPTS: u32 = 3;
    /// Pause between attempts, so a momentary network blip has time to clear.
    const PIP_INSTALL_RETRY_DELAY: Duration = Duration::from_secs(5);

    update_setup_state(app_handle, SetupState::new("installing-deps", caller_message));

    // Snapshot the file before pip reads it, so the marker records exactly
    // the dependency set that was installed even if the file is edited
    // while the (multi-minute) install runs.
    let requirements = fs::read_to_string(requirements_path());

    let venv_python = venv_python_path();

    for attempt in 1..=PIP_INSTALL_MAX_ATTEMPTS {
        // Announce the retry *before* sleeping, so the user is looking at an
        // explanation during the pause rather than at a stalled progress line.
        if attempt > 1 {
            update_setup_state_keeping_log(
                app_handle,
                "installing-deps",
                format!("{caller_message}（再試行しています {attempt}/{PIP_INSTALL_MAX_ATTEMPTS}）…"),
            );
            append_setup_log(
                app_handle,
                format!("[raw-setup] retrying pip install (attempt {attempt}/{PIP_INSTALL_MAX_ATTEMPTS})"),
            );
            thread::sleep(PIP_INSTALL_RETRY_DELAY);
        }

        // A `Command` is consumed by `spawn`, so each attempt needs its own.
        let mut pip_command = Command::new(&venv_python);
        pip_command
            .args(["-m", "pip", "install", "-r"])
            .arg(requirements_path())
            .current_dir(repo_root());

        let pip_child = match spawn_setup_step(&mut pip_command) {
            Ok(child) => child,
            Err(err) => {
                // Not retried: the interpreter itself is unusable.
                eprintln!("[raw-setup] failed to spawn {}: {err}", venv_python.display());
                update_setup_state(
                    app_handle,
                    SetupState::new("failed", format!("pip install の起動に失敗しました: {err}")),
                );
                return false;
            }
        };

        let status = match stream_setup_output(app_handle, pip_child) {
            Ok(status) => status,
            Err(err) => {
                // Not retried: the wait failed, so pip's own result is unknown.
                eprintln!("[raw-setup] failed to wait for pip install: {err}");
                update_setup_state(
                    app_handle,
                    SetupState::new("failed", format!("依存ライブラリのインストール中にエラーが発生しました: {err}")),
                );
                return false;
            }
        };

        // The only way out of this loop to the marker write below.
        if status.success() {
            break;
        }

        eprintln!(
            "[raw-setup] pip install failed (attempt {attempt}/{PIP_INSTALL_MAX_ATTEMPTS}): {status}"
        );

        if attempt == PIP_INSTALL_MAX_ATTEMPTS {
            let message = {
                let state = app_handle.state::<Mutex<SetupState>>();
                let guard = match state.lock() {
                    Ok(guard) => guard,
                    Err(poisoned) => poisoned.into_inner(),
                };
                format!(
                    "依存ライブラリのインストールに失敗しました (終了コード {}, {attempt}回試行).\n{}",
                    status.code().map(|c| c.to_string()).unwrap_or_else(|| "不明".to_string()),
                    log_tail(&guard.log, 5)
                )
            };
            // Keep the log: `SetupState::new` would reset it to empty, throwing away
            // every pip line the user has been watching — and `message` above is built
            // from `log_tail`, so the detail it points at would vanish with it.
            update_setup_state_keeping_log(app_handle, "failed", message);
            return false;
        }
    }

    // Only now — pip exited 0 — may the venv be recorded as satisfying this
    // requirements file. A write failure here is not fatal: the next launch
    // just sees a mismatch and re-runs an install that has nothing left to
    // do (pip is idempotent), which is the safe direction to fail in.
    match requirements {
        Ok(requirements) => {
            if let Err(err) = fs::write(requirements_marker_path(), requirements) {
                eprintln!(
                    "[raw-setup] failed to write {} ({err}); dependencies will be re-checked on the next launch",
                    requirements_marker_path().display()
                );
            }
        }
        Err(err) => eprintln!(
            "[raw-setup] could not read {} ({err}); skipping the marker write",
            requirements_path().display()
        ),
    }

    true
}

/// Re-runs `pip install` inside an already-existing `.venv` whose
/// `requirements-raw.txt` no longer matches what was last installed, then
/// starts the RAW engine.
///
/// # Why this exists
///
/// The `.setup()` hook used to treat "`.venv/bin/python` exists" as "the
/// venv is complete", so an app update that adds a package to
/// `requirements-raw.txt` never reached anyone who had launched the app
/// even once — their venv kept whatever it was first built with, forever,
/// and the feature needing the new package failed at runtime (for the
/// Google Drive upload that shipped this way: the button was enabled but
/// every attempt returned `dependencies_missing`). Comparing the
/// requirements file against the marker written by `run_pip_install` closes
/// that gap without recreating the venv, which is unnecessary — only the
/// package set changed, not the interpreter.
///
/// # Why a failure here does not block the window
///
/// `run_setup_bootstrap` stops at the `"failed"` stage without starting the
/// engine, which is right when there are no dependencies at all. Here the
/// user has a working install from yesterday, so a transient failure (no
/// network on launch, a pip index hiccup) must not cost them the whole app:
/// the frontend renders `"failed"` as a dead-end gate with no way past it.
/// So the engine is started regardless and the stage lands on `"ready"`,
/// leaving the app exactly as capable as it was before this check existed —
/// everything works except whatever needs the new package. The failure is
/// still reported on stderr and kept in `SetupState` (message plus the pip
/// log) for `setup_status`, and because the marker was not written, the
/// next launch retries the install on its own.
///
/// Must run off the main thread, for the same reason as
/// `run_setup_bootstrap`: `.setup()` has to return promptly so the window
/// can open. The engine is started only *after* pip finishes — starting it
/// first would run the server against a venv being mutated underneath it,
/// and it would not pick up the new packages without a restart anyway.
fn run_dependency_refresh(app_handle: AppHandle) {
    if run_pip_install(&app_handle, "アプリの更新にあわせてライブラリを追加インストールしています…") {
        update_setup_state(&app_handle, SetupState::new("succeeded", "ライブラリの更新が完了しました。"));
    } else {
        update_setup_state_keeping_log(
            &app_handle,
            "ready",
            "ライブラリの更新に失敗しました。次回起動時に再試行します（一部の機能が使えない場合があります）。",
        );
    }

    let child = spawn_raw_engine(&app_handle);
    let state = app_handle.state::<RawEngineState>();
    let mut guard = match state.0.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    *guard = child;
}

/// Spawns a background thread that watches for `SIGTERM`/`SIGINT`/`SIGQUIT`
/// delivered directly to this process and reaps the RAW-engine child before
/// the process disappears.
///
/// # Why this exists
///
/// Tauri's `RunEvent::ExitRequested`/`RunEvent::Exit` handlers (wired up in
/// `main` below) only fire for *application-level* quit paths that go
/// through Tauri's own event loop: Cmd+Q, closing the last window, or an
/// explicit `app.exit()`/`AppHandle::exit()` call. They do **not** fire when
/// the OS process itself is terminated from the outside — e.g. `kill <pid>`,
/// `kill -TERM <pid>`, a process supervisor stopping the app, or Ctrl+C on
/// the `cargo run`/`tauri dev` process that launched this binary. In that
/// case winit's event loop is torn down by the signal before it ever gets a
/// chance to synthesize an exit event, so `kill_raw_engine()` is never
/// invoked and `python -m local_raw.server` is orphaned, left running and
/// holding port 8788.
///
/// Reproduced exactly: `npm run tauri:dev`, confirmed the sidecar was up via
/// `curl 127.0.0.1:8788/api/health`, then sent `SIGTERM` to the app process
/// tree — the python process was still running afterward (only died when
/// killed manually).
///
/// This is a belt-and-suspenders safety net *alongside* the existing
/// `RunEvent` handlers, not a replacement for them: those still fire (and
/// still matter) for the in-app quit paths, and this thread is the fallback
/// for everything else. `kill_raw_engine` is idempotent and briefly locks a
/// plain (non-poisonable-in-practice) `Mutex`, so it is safe for both this
/// thread and the `RunEvent` handlers to call it — worst case one call finds
/// the child already reaped and no-ops.
fn spawn_signal_cleanup_thread(app_handle: AppHandle) {
    let mut signals = match Signals::new(TERM_SIGNALS) {
        Ok(signals) => signals,
        Err(err) => {
            eprintln!(
                "[raw-engine] failed to install SIGTERM/SIGINT handler; the RAW \
                 engine will be orphaned if this process is killed externally: {err}"
            );
            return;
        }
    };

    thread::spawn(move || {
        // Blocks the dedicated thread (not an actual OS signal handler —
        // signal-hook defers delivery to here) until a termination signal
        // arrives, so ordinary mutex locking and I/O below are safe.
        if signals.forever().next().is_some() {
            eprintln!(
                "[raw-engine] received termination signal, cleaning up RAW engine before exit"
            );
            let state = app_handle.state::<RawEngineState>();
            kill_raw_engine(&state);
            // This path only runs for a raw OS signal, not a Tauri-mediated
            // quit: winit's event loop has no guarantee it will unwind or
            // fire RunEvent::Exit on its own after a signal, so force the
            // exit now that cleanup is done.
            std::process::exit(0);
        }
    });
}

fn main() {
    let app = tauri::Builder::default()
        .manage(RawEngineState(Mutex::new(None)))
        .manage(Mutex::new(SetupState::new("ready", "")))
        .invoke_handler(tauri::generate_handler![
            setup_status,
            // Core Image プレビュー現像（Issue #72 / #73）。GEOFRAME_PREVIEW_ENGINE=rawpy で切り戻し。
            coreimage::preview_engine,
            coreimage::develop_preview_coreimage,
            // 「エンジン準備完了」の判定（Issue #109）。Python の /api/health の代わり。
            coreimage::engine_info,
            // プリセット定義（Issue #80）。定義の正は Rust 側。Python の /api/health は移行期間中の予備。
            presets::get_presets
        ])
        // Native folder picker for the source-folder setting. The frontend
        // calls `open({ directory: true })` from @tauri-apps/plugin-dialog;
        // the matching `dialog:allow-open` permission lives in
        // capabilities/default.json. Browser mode (`npm run dev`) has no
        // plugin at all and falls back to a path text field instead.
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let app_handle = app.handle().clone();

            if venv_python_path().is_file() {
                if dependencies_up_to_date() {
                    // Common case on a dev machine that already ran
                    // `npm run setup:raw`: no bootstrap needed, behave
                    // exactly as before. Still emit a deterministic "ready"
                    // state so the frontend always gets a first
                    // `"raw-setup"` signal either way, rather than having to
                    // distinguish "no bootstrap needed" from "haven't heard
                    // anything yet".
                    update_setup_state(
                        &app_handle,
                        SetupState::new("ready", "セットアップは不要です。エンジンを起動しています…"),
                    );

                    let child = spawn_raw_engine(&app_handle);
                    let state = app.state::<RawEngineState>();
                    let mut guard = match state.0.lock() {
                        Ok(guard) => guard,
                        Err(poisoned) => poisoned.into_inner(),
                    };
                    *guard = child;
                } else {
                    // The venv exists but predates the current
                    // `requirements-raw.txt` (an app update added a
                    // package). Install the difference on a background
                    // thread, which then starts the engine itself — see
                    // run_dependency_refresh's doc comment.
                    thread::spawn(move || run_dependency_refresh(app_handle));
                }
            } else {
                // No venv yet: bootstrap it on a background thread (see
                // run_setup_bootstrap's doc comment) and let it spawn the
                // RAW engine itself once dependencies are installed.
                thread::spawn(move || run_setup_bootstrap(app_handle));
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // Safety net for external termination (kill/Ctrl+C on the process
    // itself) — see spawn_signal_cleanup_thread's doc comment for why this
    // is needed alongside the RunEvent handlers below.
    spawn_signal_cleanup_thread(app.handle().clone());

    app.run(|app_handle, event| match event {
        // Fired once the user (or OS) requests the app close, including
        // "all windows closed" on platforms where that triggers exit.
        RunEvent::ExitRequested { .. } => {
            let state = app_handle.state::<RawEngineState>();
            kill_raw_engine(&state);
        }
        // Final catch-all right before the process actually exits; kept as
        // a safety net in case ExitRequested is ever intercepted/prevented
        // upstream. kill_raw_engine is idempotent, so this is a no-op if
        // ExitRequested already reaped the child.
        RunEvent::Exit => {
            let state = app_handle.state::<RawEngineState>();
            kill_raw_engine(&state);
        }
        _ => {}
    });
}
