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

use std::env;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;

use signal_hook::consts::TERM_SIGNALS;
use signal_hook::iterator::Signals;
use tauri::{AppHandle, Manager, RunEvent};

/// Holds the spawned RAW-engine child process, if any. Managed as Tauri
/// app state so both the `setup` hook (spawn) and the exit handler (kill)
/// can reach it.
struct RawEngineState(Mutex<Option<Child>>);

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

/// Resolves the Python interpreter used to run the RAW engine.
///
/// Priority:
/// 1. `RAW_ASSIST_PYTHON` env var, if set (escape hatch for custom
///    interpreters/venvs).
/// 2. `<repoRoot>/.venv/bin/python`, the venv created by `npm run setup:raw`.
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
    repo_root().join(".venv").join("bin").join("python")
}

/// Spawns `python -m local_raw.server` with `current_dir` set to the repo
/// root (server.py itself also resolves `PROJECT_ROOT` independently of
/// cwd, but we match it anyway for predictable relative-path behavior).
///
/// Never panics: a missing venv or a spawn failure is logged to stderr and
/// the shell keeps going with `None`, matching the frontend's existing
/// "engine unreachable" health-check gate.
fn spawn_raw_engine() -> Option<Child> {
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

    match Command::new(&python)
        .args(["-m", "local_raw.server"])
        .current_dir(repo_root())
        .stdin(Stdio::null())
        .spawn()
    {
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
        .setup(|app| {
            let child = spawn_raw_engine();
            let state = app.state::<RawEngineState>();
            let mut guard = match state.0.lock() {
                Ok(guard) => guard,
                Err(poisoned) => poisoned.into_inner(),
            };
            *guard = child;
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
