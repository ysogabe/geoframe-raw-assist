//! Core Image 試験現像（Issue #65 PR2）。
//!
//! Python サーバ / `.venv` / localhost HTTP を一切経由せず、Rust から Swift CLI
//! `geoframe-ci-develop`（`native/coreimage-cli`、Tauri の externalBin）を子プロセスとして
//! 直接呼ぶ。CLI の入出力契約は `native/coreimage-cli/README.md` と
//! `docs/adr/0001-coreimage-raw-poc.md` を参照。
//!
//! - 環境変数 `GEOFRAME_COREIMAGE_EXPERIMENT=1` のときだけ有効。無効なら
//!   `develop_coreimage` 自体を拒否する（UI からボタンを消すだけに頼らない）。
//!   Python 側の `config.json` にはフラグを足さない。
//! - `config.json`（Python の `local_raw/config.py` と同じファイル）は `source_root` を
//!   **読むだけ**。書き込みは Python 側の責務のまま。
//! - capture_id → ARW の解決は `local_raw/source_files.py` と同じ規則の再実装。
//! - 出力先は `<app_data_dir>/coreimage/` 配下で Rust が決める（フロントからパスは受けない）。
//!   元の ARW / JPG と、Python 側のキャッシュ・書き出し先には書かない。

use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, UNIX_EPOCH};

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};

/// 試験機能を有効にする環境変数。値がちょうど `"1"` のときだけ有効。
pub const EXPERIMENT_ENV: &str = "GEOFRAME_COREIMAGE_EXPERIMENT";

/// externalBin の名前（tauri-build が実行ファイルの隣へ triple 無しの名前でコピーする）。
const SIDECAR_NAME: &str = "geoframe-ci-develop";

/// 出力ディレクトリ名（`<app_data_dir>/coreimage/`）。tauri.conf.json の
/// `assetProtocol.scope`（`$APPDATA/coreimage/**`）と一致させること。
const OUTPUT_DIR_NAME: &str = "coreimage";

/// 現像 1 回のウォッチドッグ。超えたら子プロセスを kill してエラーにする。
const DEVELOP_TIMEOUT: Duration = Duration::from_secs(60);
/// `--version` は即答するはずなので短めに切る。
const VERSION_TIMEOUT: Duration = Duration::from_secs(10);

/// アプリから CLI へ渡す固定値。decoder_version は OS 更新で既定値が変わりうるので明示する
/// （README「アプリからは明示指定を推奨」）。
const DECODER_VERSION: &str = "9";
const PROFILE: &str = "neutral";
const PREVIEW_MAX_EDGE: u32 = 2400;
const FORMAT: &str = "jpeg";

/// 古い一時ファイルとみなすまでの時間（掃除の対象。`cleanup_stale_temps`）。
const STALE_TEMP_AGE: Duration = Duration::from_secs(10 * 60);

/// キャッシュ名ハッシュの入力形式の版。ハッシュ入力の組み立てを変えたら上げる。
const CACHE_KEY_SCHEMA: u32 = 1;

pub fn experiment_enabled() -> bool {
    matches!(std::env::var(EXPERIMENT_ENV).as_deref(), Ok("1"))
}

/// フロントへ返すエラー。CLI の `error.code` / `message` はそのまま詰め替える。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CoreImageError {
    pub code: String,
    pub message: String,
}

impl CoreImageError {
    fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }
}

/// 現像モード。自由文字列を CLI の JSON へ流さないよう enum で受ける。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DevelopMode {
    Preview,
    Full,
}

impl DevelopMode {
    fn as_str(self) -> &'static str {
        match self {
            DevelopMode::Preview => "preview",
            DevelopMode::Full => "full",
        }
    }
}

/// `develop_coreimage` の応答（フロントの `CoreImageResult` と一致させること）。
#[derive(Debug, Clone, Serialize)]
pub struct CoreImageResult {
    pub capture_id: String,
    pub mode: DevelopMode,
    /// 出力 JPEG の絶対パス（フロントは `convertFileSrc` で表示 URL にする）。
    pub output_path: String,
    pub width: u64,
    pub height: u64,
    /// CLI の `elapsed_ms`（`decode` / `render_encode` / `total`）。キャッシュヒット時は
    /// 最初に現像したときの値。
    pub elapsed_ms: Value,
    pub cache_hit: bool,
    pub decoder_version: String,
    pub cli_version: String,
}

// ---------------------------------------------------------------------------
// 設定（source_root）の読み込み
// ---------------------------------------------------------------------------

/// `<app_data_dir>/config.json` の `source_root` を読む（書き込まない）。
pub fn read_source_root(app_data_dir: &Path) -> Result<PathBuf, CoreImageError> {
    let config_path = app_data_dir.join("config.json");
    let text = match fs::read_to_string(&config_path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(CoreImageError::new(
                "SOURCE_ROOT_NOT_CONFIGURED",
                "現像元フォルダが未設定です（先にアプリで現像元フォルダを選んでください）",
            ));
        }
        Err(error) => {
            return Err(CoreImageError::new(
                "CONFIG_UNREADABLE",
                format!(
                    "設定ファイルを読めません: {}: {error}",
                    config_path.display()
                ),
            ));
        }
    };
    let value: Value = serde_json::from_str(&text).map_err(|error| {
        CoreImageError::new(
            "CONFIG_INVALID",
            format!("設定ファイルが JSON として読めません: {error}"),
        )
    })?;
    match value.get("source_root").and_then(Value::as_str) {
        Some(root) if !root.is_empty() && Path::new(root).is_absolute() => Ok(PathBuf::from(root)),
        _ => Err(CoreImageError::new(
            "SOURCE_ROOT_NOT_CONFIGURED",
            "設定ファイルに現像元フォルダ（source_root の絶対パス）がありません",
        )),
    }
}

// ---------------------------------------------------------------------------
// capture_id → ARW の解決（local_raw/source_files.py と同じ規則）
// ---------------------------------------------------------------------------

fn capture_id_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    // `\d` は Unicode 数字にも当たるので ASCII に絞る（入口の検査なので厳しい側に倒す）。
    PATTERN.get_or_init(|| Regex::new(r"^DSC[0-9]{5}$").expect("valid regex"))
}

fn entry_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    // ステムは大文字固定、拡張子だけ大文字小文字を問わない（source_files._ENTRY_PATTERN）。
    PATTERN.get_or_init(|| Regex::new(r"^(DSC[0-9]{5})\.((?i:ARW|JPG))$").expect("valid regex"))
}

pub fn is_valid_capture_id(capture_id: &str) -> bool {
    capture_id_pattern().is_match(capture_id)
}

/// 同じ ID の ARW 候補（実エントリ名）から 1 つを決定的に選ぶ。
/// 正規表記 `<ID>.ARW` を最優先し、なければ実名の辞書順で最初のもの（source_files._pick）。
pub fn pick_arw_entry(candidates: &[String], capture_id: &str) -> Option<String> {
    let canonical = format!("{capture_id}.ARW");
    if candidates.contains(&canonical) {
        return Some(canonical);
    }
    candidates.iter().min().cloned()
}

/// `root` 直下から `capture_id` の ARW を実エントリ名で解決し、canonicalize したパスを返す。
///
/// シンボリックリンクでフォルダ外を指すもの（解決後の親が root と一致しないもの）は拒否する。
pub fn resolve_arw(root: &Path, capture_id: &str) -> Result<PathBuf, CoreImageError> {
    if !is_valid_capture_id(capture_id) {
        return Err(CoreImageError::new(
            "INVALID_CAPTURE_ID",
            format!("撮影IDの形式が不正です: {capture_id:?}"),
        ));
    }
    let root = root.canonicalize().map_err(|error| {
        CoreImageError::new(
            "SOURCE_ROOT_NOT_FOUND",
            format!("現像元フォルダを開けません: {}: {error}", root.display()),
        )
    })?;
    if !root.is_dir() {
        return Err(CoreImageError::new(
            "SOURCE_ROOT_NOT_FOUND",
            format!(
                "現像元フォルダがディレクトリではありません: {}",
                root.display()
            ),
        ));
    }
    let entries = fs::read_dir(&root).map_err(|error| {
        CoreImageError::new(
            "SOURCE_ROOT_NOT_FOUND",
            format!("現像元フォルダを読めません: {}: {error}", root.display()),
        )
    })?;
    let mut candidates = Vec::new();
    for entry in entries.flatten() {
        // 非 UTF-8 名は対象パターンに当たり得ないので飛ばす。
        let Ok(name) = entry.file_name().into_string() else {
            continue;
        };
        if let Some(found) = entry_pattern().captures(&name) {
            if &found[1] == capture_id && found[2].eq_ignore_ascii_case("ARW") {
                candidates.push(name);
            }
        }
    }
    let Some(name) = pick_arw_entry(&candidates, capture_id) else {
        return Err(CoreImageError::new(
            "ARW_NOT_FOUND",
            format!("{capture_id} の ARW が現像元フォルダにありません"),
        ));
    };
    let resolved = root.join(&name).canonicalize().map_err(|error| {
        CoreImageError::new("ARW_NOT_FOUND", format!("{name} を開けません: {error}"))
    })?;
    if resolved.parent() != Some(root.as_path()) {
        return Err(CoreImageError::new(
            "ARW_OUTSIDE_SOURCE_ROOT",
            format!("{name} は現像元フォルダの外を指しています"),
        ));
    }
    if !resolved.is_file() {
        return Err(CoreImageError::new(
            "ARW_NOT_FOUND",
            format!("{name} は通常ファイルではありません"),
        ));
    }
    Ok(resolved)
}

// ---------------------------------------------------------------------------
// 子プロセス（ウォッチドッグ付き）
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct SidecarOutput {
    status: ExitStatus,
    stdout: String,
    stderr: String,
}

fn read_all_in_thread<R: Read + Send + 'static>(mut reader: R) -> thread::JoinHandle<String> {
    thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = reader.read_to_end(&mut buffer);
        String::from_utf8_lossy(&buffer).into_owned()
    })
}

/// `program` を起動し、`stdin_bytes` を書いて **stdin を閉じ**、`timeout` 以内の終了を待つ。
///
/// - stdin を閉じないと CLI の `readDataToEndOfFile()` に EOF が来ず永久に待つ。
/// - stdout / stderr は別スレッドで読み切る（パイプが詰まって子が止まり、ウォッチドッグまで
///   無駄に待つのを防ぐ）。
/// - `timeout` を過ぎたら kill して `SIDECAR_TIMEOUT`。
fn run_sidecar(
    program: &Path,
    args: &[&str],
    stdin_bytes: Option<&[u8]>,
    timeout: Duration,
) -> Result<SidecarOutput, CoreImageError> {
    let mut child = Command::new(program)
        .args(args)
        .stdin(if stdin_bytes.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| {
            CoreImageError::new(
                "SIDECAR_SPAWN_FAILED",
                format!("{} を起動できません: {error}", program.display()),
            )
        })?;

    let stdout_reader = child.stdout.take().map(read_all_in_thread);
    let stderr_reader = child.stderr.take().map(read_all_in_thread);

    if let Some(bytes) = stdin_bytes {
        // take() で取り出してスコープ末尾で drop する = EOF を送る。
        if let Some(mut stdin) = child.stdin.take() {
            if let Err(error) = stdin.write_all(bytes) {
                let _ = child.kill();
                let _ = child.wait();
                return Err(CoreImageError::new(
                    "SIDECAR_IO_FAILED",
                    format!("CLI へのリクエスト送信に失敗しました: {error}"),
                ));
            }
        }
    }

    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(CoreImageError::new(
                    "SIDECAR_TIMEOUT",
                    format!(
                        "Core Image CLI が {} 秒以内に終わらなかったため中断しました",
                        timeout.as_secs_f32()
                    ),
                ));
            }
            Ok(None) => thread::sleep(Duration::from_millis(25)),
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(CoreImageError::new(
                    "SIDECAR_IO_FAILED",
                    format!("CLI の終了待ちに失敗しました: {error}"),
                ));
            }
        }
    };

    let stdout = stdout_reader
        .and_then(|handle| handle.join().ok())
        .unwrap_or_default();
    let stderr = stderr_reader
        .and_then(|handle| handle.join().ok())
        .unwrap_or_default();
    Ok(SidecarOutput {
        status,
        stdout,
        stderr,
    })
}

/// stdout の最後の非空行を JSON として読む（契約上 stdout は JSON 1 行だけ）。
fn parse_response(output: &SidecarOutput) -> Result<Value, CoreImageError> {
    let line = output
        .stdout
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("");
    serde_json::from_str::<Value>(line).map_err(|_| {
        let stderr_tail: String = output
            .stderr
            .lines()
            .rev()
            .take(5)
            .collect::<Vec<_>>()
            .join(" / ");
        CoreImageError::new(
            "SIDECAR_BAD_RESPONSE",
            format!(
                "Core Image CLI の応答を読めません（終了状態 {}）: {stderr_tail}",
                output.status
            ),
        )
    })
}

/// CLI の失敗応答 `{"ok":false,"error":{"code","message"}}` をそのまま詰め替える。
fn cli_error(response: &Value, status: ExitStatus) -> CoreImageError {
    let error = response.get("error");
    let code = error.and_then(|e| e.get("code")).and_then(Value::as_str);
    let message = error.and_then(|e| e.get("message")).and_then(Value::as_str);
    match (code, message) {
        (Some(code), Some(message)) => CoreImageError::new(code, message),
        _ => CoreImageError::new(
            "SIDECAR_BAD_RESPONSE",
            format!("Core Image CLI が失敗しました（終了状態 {status}）"),
        ),
    }
}

// ---------------------------------------------------------------------------
// CLI バージョン（キャッシュキーの一部）
// ---------------------------------------------------------------------------

/// `--version` の応答 1 行（cli_version + macOS バージョンを含む）と cli_version。
#[derive(Debug, Clone)]
struct CliVersion {
    fingerprint: String,
    cli_version: String,
}

/// sidecar ごとに `--version` の結果を覚えておく（プロセス寿命中は変わらない前提）。
/// 失敗は覚えない（次回また試す）。
fn cli_version(sidecar: &Path) -> Result<CliVersion, CoreImageError> {
    static CACHE: Mutex<Option<(PathBuf, CliVersion)>> = Mutex::new(None);
    {
        let guard = CACHE
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some((path, version)) = guard.as_ref() {
            if path == sidecar {
                return Ok(version.clone());
            }
        }
    }
    let output = run_sidecar(sidecar, &["--version"], None, VERSION_TIMEOUT)?;
    let response = parse_response(&output)?;
    if response.get("ok") != Some(&Value::Bool(true)) || !output.status.success() {
        return Err(cli_error(&response, output.status));
    }
    let cli_version = response
        .get("cli_version")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_string();
    // JSON を正規化し直して指紋にする（空白や改行の差でキャッシュが割れないように）。
    let version = CliVersion {
        fingerprint: response.to_string(),
        cli_version,
    };
    let mut guard = CACHE
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    *guard = Some((sidecar.to_path_buf(), version.clone()));
    Ok(version)
}

// ---------------------------------------------------------------------------
// 現像本体
// ---------------------------------------------------------------------------

/// CLI へのリクエスト（`output` 以外）。`output` はこれのハッシュから決まるので後で足す。
fn base_request(input: &Path, mode: DevelopMode) -> serde_json::Map<String, Value> {
    let mut request = serde_json::Map::new();
    request.insert(
        "input".into(),
        Value::String(input.to_string_lossy().into_owned()),
    );
    request.insert("mode".into(), Value::String(mode.as_str().into()));
    if mode == DevelopMode::Preview {
        // full で max_edge を付けると CLI は INVALID_REQUEST を返す。
        request.insert("max_edge".into(), json!(PREVIEW_MAX_EDGE));
    }
    request.insert("format".into(), Value::String(FORMAT.into()));
    request.insert(
        "decoder_version".into(),
        Value::String(DECODER_VERSION.into()),
    );
    request.insert("settings".into(), json!({ "profile": PROFILE }));
    request
}

/// キャッシュ名に使う安定ハッシュ（SHA-256 の先頭 16 桁）。
///
/// std の `DefaultHasher` は実行ごとに値が変わりうるので使わない。入力は
/// リクエスト（output 以外、input は canonicalize 済みの絶対パス）+ CLI の `--version` 応答 +
/// ARW の同一性（サイズ + mtime）。パスを含めるのは、`capture_id` がカード／フォルダを
/// またいで衝突するため（config.py の docstring 参照）。
fn cache_key(
    request: &serde_json::Map<String, Value>,
    version: &CliVersion,
    size: u64,
    mtime_ns: u128,
) -> String {
    // serde_json の Map はキー順が決定的（BTreeMap、preserve_order 時は挿入順で固定）。
    let payload = json!({
        "schema": CACHE_KEY_SCHEMA,
        "request": request,
        "cli": version.fingerprint,
        "input_size": size,
        "input_mtime_ns": mtime_ns.to_string(),
    });
    let digest = Sha256::digest(payload.to_string().as_bytes());
    digest
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn result_from_response(
    response: &Value,
    capture_id: &str,
    mode: DevelopMode,
    output: &Path,
    cache_hit: bool,
    version: &CliVersion,
) -> Option<CoreImageResult> {
    Some(CoreImageResult {
        capture_id: capture_id.to_string(),
        mode,
        output_path: output.to_string_lossy().into_owned(),
        width: response.get("width")?.as_u64()?,
        height: response.get("height")?.as_u64()?,
        elapsed_ms: response.get("elapsed_ms").cloned().unwrap_or(Value::Null),
        cache_hit,
        decoder_version: response
            .get("decoder_version")
            .and_then(Value::as_str)
            .unwrap_or(DECODER_VERSION)
            .to_string(),
        cli_version: version.cli_version.clone(),
    })
}

/// 来歴 JSON の一時ファイル名。pid だけだと同一プロセス内の並行書き込みで衝突しうるので、
/// プロセス内の通し番号も入れる。先頭ドット + `.tmp` は `cleanup_stale_temps` の掃除対象。
fn provenance_temp_name(file_name: &str) -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let sequence = COUNTER.fetch_add(1, Ordering::Relaxed);
    format!(".{file_name}.{}.{sequence}.tmp", std::process::id())
}

/// `dir` 直下の、先頭ドットかつ `.tmp` で終わる通常ファイルのうち、最終更新から `max_age`
/// 以上経ったものを消す。CLI をタイムアウトで kill すると、CLI が output と同じディレクトリに
/// 作る `.<output名>.<UUID>.tmp` が残るため。シンボリックリンクは追わず（symlink_metadata）、
/// 削除の失敗は無視する（掃除は best effort）。
fn cleanup_stale_temps(dir: &Path, max_age: Duration) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    let now = std::time::SystemTime::now();
    for entry in entries.flatten() {
        let Ok(name) = entry.file_name().into_string() else {
            continue;
        };
        if !(name.starts_with('.') && name.ends_with(".tmp")) {
            continue;
        }
        let path = entry.path();
        let Ok(metadata) = fs::symlink_metadata(&path) else {
            continue;
        };
        if !metadata.file_type().is_file() {
            continue;
        }
        let old_enough = metadata
            .modified()
            .ok()
            .and_then(|modified| now.duration_since(modified).ok())
            .is_some_and(|age| age >= max_age);
        if old_enough {
            let _ = fs::remove_file(&path);
        }
    }
}

/// 同時に走る現像を 1 本に制限するための旗（プロセス全体）。
static DEVELOP_RUNNING: AtomicBool = AtomicBool::new(false);

/// 現像枠の RAII ガード。drop（正常終了・エラーの `?`・panic の巻き戻し）で必ず枠を返す。
struct DevelopSlot<'a>(&'a AtomicBool);

impl<'a> DevelopSlot<'a> {
    /// 空いていれば枠を取る。使用中なら None（待たない）。
    fn try_acquire(flag: &'a AtomicBool) -> Option<Self> {
        flag.compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .ok()
            .map(|_| DevelopSlot(flag))
    }
}

impl Drop for DevelopSlot<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

/// 来歴 JSON を一時ファイル経由で原子的に書く。
fn write_provenance(path: &Path, body: &str) -> Result<(), CoreImageError> {
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temp = path.with_file_name(provenance_temp_name(&file_name));
    let result = fs::write(&temp, body).and_then(|_| fs::rename(&temp, path));
    if let Err(error) = result {
        let _ = fs::remove_file(&temp);
        return Err(CoreImageError::new(
            "PROVENANCE_WRITE_FAILED",
            format!("来歴 JSON を書けません: {error}"),
        ));
    }
    Ok(())
}

/// 現像の本体（Tauri に依存しない。テストから直接呼ぶ）。
pub fn develop(
    app_data_dir: &Path,
    sidecar: &Path,
    capture_id: &str,
    mode: DevelopMode,
    timeout: Duration,
) -> Result<CoreImageResult, CoreImageError> {
    // 1. 入力の解決（設定 → ARW）。
    let source_root = read_source_root(app_data_dir)?;
    let input = resolve_arw(&source_root, capture_id)?;
    let metadata = fs::metadata(&input).map_err(|error| {
        CoreImageError::new(
            "ARW_NOT_FOUND",
            format!("{} を読めません: {error}", input.display()),
        )
    })?;
    let mtime_ns = metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);

    // 2. 出力名（安定ハッシュ）の決定。
    let version = cli_version(sidecar)?;
    let mut request = base_request(&input, mode);
    let key = cache_key(&request, &version, metadata.len(), mtime_ns);

    let output_dir = app_data_dir.join(OUTPUT_DIR_NAME);
    fs::create_dir_all(&output_dir).map_err(|error| {
        CoreImageError::new(
            "OUTPUT_DIR_FAILED",
            format!("出力先を作れません: {}: {error}", output_dir.display()),
        )
    })?;
    let output_dir = output_dir.canonicalize().map_err(|error| {
        CoreImageError::new(
            "OUTPUT_DIR_FAILED",
            format!("出力先を解決できません: {error}"),
        )
    })?;
    // タイムアウトで kill された CLI などが残した古い一時ファイルを掃除する。
    cleanup_stale_temps(&output_dir, STALE_TEMP_AGE);
    let stem = format!("{capture_id}_{}_{key}", mode.as_str());
    let output = output_dir.join(format!("{stem}.jpg"));
    let provenance = output_dir.join(format!("{stem}.json"));
    // 出力は必ず coreimage ディレクトリ直下（ARW のフォルダや他のキャッシュへ書かない）。
    assert_eq!(output.parent(), Some(output_dir.as_path()));
    assert_eq!(provenance.parent(), Some(output_dir.as_path()));

    // 3. キャッシュヒット: 画像と来歴の両方があるときだけ（画像だけなら来歴の書き込みが
    //    途中で落ちたとみなして現像し直す）。
    if output.is_file() {
        if let Ok(text) = fs::read_to_string(&provenance) {
            if let Ok(saved) = serde_json::from_str::<Value>(&text) {
                if let Some(result) =
                    result_from_response(&saved, capture_id, mode, &output, true, &version)
                {
                    return Ok(result);
                }
            }
        }
    }

    // 4. 同時実行は 1 本まで。キャッシュヒットは上で返しているので、ここで止めるのは
    //    実際に CLI を走らせる呼び出しだけ。待たずに BUSY を返す（枠はガードの drop で返る）。
    let _slot = DevelopSlot::try_acquire(&DEVELOP_RUNNING).ok_or_else(|| {
        CoreImageError::new(
            "BUSY",
            "別の Core Image 現像を実行中です。終わってからもう一度試してください",
        )
    })?;

    // 5. CLI 呼び出し。
    request.insert(
        "output".into(),
        Value::String(output.to_string_lossy().into_owned()),
    );
    let body = Value::Object(request).to_string();
    let result = run_sidecar(sidecar, &[], Some(body.as_bytes()), timeout)?;
    let response = parse_response(&result)?;
    if response.get("ok") != Some(&Value::Bool(true)) || !result.status.success() {
        return Err(cli_error(&response, result.status));
    }
    let reported = response
        .get("output")
        .and_then(Value::as_str)
        .map(PathBuf::from);
    if reported.as_deref() != Some(output.as_path()) || !output.is_file() {
        return Err(CoreImageError::new(
            "SIDECAR_BAD_RESPONSE",
            "Core Image CLI の出力先が依頼と一致しないか、出力ファイルがありません",
        ));
    }
    let developed = result_from_response(&response, capture_id, mode, &output, false, &version)
        .ok_or_else(|| {
            CoreImageError::new(
                "SIDECAR_BAD_RESPONSE",
                "Core Image CLI の応答に width / height がありません",
            )
        })?;

    // 6. 来歴: CLI の応答 JSON をそのまま横に置く。
    write_provenance(&provenance, &format!("{}\n", response))?;
    Ok(developed)
}

/// アプリ実行時の sidecar パス。tauri-build が externalBin を実行ファイルの隣へコピーする
/// （dev は `target/debug/`、.app は `Contents/MacOS/`）。
fn sidecar_path() -> Result<PathBuf, CoreImageError> {
    let exe = std::env::current_exe().map_err(|error| {
        CoreImageError::new(
            "SIDECAR_NOT_FOUND",
            format!("実行ファイルの場所を取得できません: {error}"),
        )
    })?;
    let path = exe
        .parent()
        .map(|dir| dir.join(SIDECAR_NAME))
        .ok_or_else(|| {
            CoreImageError::new(
                "SIDECAR_NOT_FOUND",
                "実行ファイルのディレクトリを取得できません",
            )
        })?;
    if !path.is_file() {
        return Err(CoreImageError::new(
            "SIDECAR_NOT_FOUND",
            format!(
                "Core Image CLI が見つかりません: {}（npm run build:coreimage を実行してください）",
                path.display()
            ),
        ));
    }
    Ok(path)
}

// ---------------------------------------------------------------------------
// Tauri コマンド
// ---------------------------------------------------------------------------

/// 試験機能が有効か（フロントはこれが true のときだけボタンを出す）。
#[tauri::command]
pub fn coreimage_experiment_enabled() -> bool {
    experiment_enabled()
}

/// 選択中カットを Core Image で現像する。JS からは
/// `invoke("develop_coreimage", { captureId, mode })`。
///
/// async コマンド + spawn_blocking にしている。同期コマンドは main thread で走るため、
/// 数秒かかる現像の間 UI が固まる。
#[tauri::command]
pub async fn develop_coreimage(
    app: AppHandle,
    capture_id: String,
    mode: DevelopMode,
) -> Result<CoreImageResult, CoreImageError> {
    if !experiment_enabled() {
        return Err(CoreImageError::new(
            "EXPERIMENT_DISABLED",
            format!("Core Image 試験現像は無効です（{EXPERIMENT_ENV}=1 で起動してください）"),
        ));
    }
    let app_data_dir = app.path().app_data_dir().map_err(|error| {
        CoreImageError::new(
            "APP_DATA_DIR_UNAVAILABLE",
            format!("アプリのデータフォルダを取得できません: {error}"),
        )
    })?;
    let sidecar = sidecar_path()?;
    tauri::async_runtime::spawn_blocking(move || {
        develop(&app_data_dir, &sidecar, &capture_id, mode, DEVELOP_TIMEOUT)
    })
    .await
    .map_err(|error| {
        CoreImageError::new(
            "INTERNAL_ERROR",
            format!("現像スレッドが異常終了しました: {error}"),
        )
    })?
}

// ---------------------------------------------------------------------------
// テスト
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    /// 一時ディレクトリ（drop で削除）。tempfile クレートを足さずに済ませる。
    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static COUNTER: AtomicUsize = AtomicUsize::new(0);
            let nanos = std::time::SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0);
            let path = std::env::temp_dir().join(format!(
                "geoframe-coreimage-test-{tag}-{}-{nanos}-{}",
                std::process::id(),
                COUNTER.fetch_add(1, Ordering::SeqCst)
            ));
            fs::create_dir_all(&path).expect("create temp dir");
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn touch(path: &Path) {
        fs::write(path, b"x").expect("write file");
    }

    #[test]
    fn capture_id_validation() {
        assert!(is_valid_capture_id("DSC04430"));
        for bad in [
            "dsc04430",
            "DSC0443",
            "DSC044300",
            "../DSC04430",
            "DSC04430.ARW",
            "DSC0443０",
            "",
            "DSC 4430",
        ] {
            assert!(!is_valid_capture_id(bad), "{bad:?} は拒否されるべき");
        }
    }

    #[test]
    fn pick_prefers_canonical_then_sorted() {
        let names = |list: &[&str]| list.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(
            pick_arw_entry(
                &names(&["DSC00001.arw", "DSC00001.ARW", "DSC00001.Arw"]),
                "DSC00001"
            )
            .as_deref(),
            Some("DSC00001.ARW")
        );
        // 正規表記が無ければ実名の辞書順で最初（"A" < "a"）。
        assert_eq!(
            pick_arw_entry(&names(&["DSC00001.arw", "DSC00001.Arw"]), "DSC00001").as_deref(),
            Some("DSC00001.Arw")
        );
        assert_eq!(pick_arw_entry(&[], "DSC00001"), None);
    }

    #[test]
    fn resolves_lowercase_extension_to_actual_entry_name() {
        let dir = TempDir::new("lower");
        touch(&dir.path().join("DSC00002.arw"));
        touch(&dir.path().join("DSC00002.JPG"));
        let resolved = resolve_arw(dir.path(), "DSC00002").expect("resolve");
        assert_eq!(
            resolved.file_name().and_then(|n| n.to_str()),
            Some("DSC00002.arw")
        );
        assert_eq!(
            resolved.parent(),
            Some(dir.path().canonicalize().unwrap().as_path())
        );
    }

    #[test]
    fn resolves_canonical_name() {
        let dir = TempDir::new("canonical");
        touch(&dir.path().join("DSC00003.ARW"));
        let resolved = resolve_arw(dir.path(), "DSC00003").expect("resolve");
        assert_eq!(
            resolved.file_name().and_then(|n| n.to_str()),
            Some("DSC00003.ARW")
        );
    }

    #[test]
    fn rejects_invalid_id_before_touching_fs() {
        let missing = Path::new("/nonexistent-geoframe-root");
        assert_eq!(
            resolve_arw(missing, "../etc").unwrap_err().code,
            "INVALID_CAPTURE_ID"
        );
        assert_eq!(
            resolve_arw(missing, "dsc00001").unwrap_err().code,
            "INVALID_CAPTURE_ID"
        );
    }

    #[test]
    fn missing_arw_and_jpg_only() {
        let dir = TempDir::new("missing");
        touch(&dir.path().join("DSC00004.JPG"));
        // 小文字ステムはパターン外（ステムは大文字固定）。
        touch(&dir.path().join("dsc00004.ARW"));
        assert_eq!(
            resolve_arw(dir.path(), "DSC00004").unwrap_err().code,
            "ARW_NOT_FOUND"
        );
        assert_eq!(
            resolve_arw(dir.path(), "DSC09999").unwrap_err().code,
            "ARW_NOT_FOUND"
        );
    }

    #[test]
    fn missing_root() {
        let dir = TempDir::new("noroot");
        let root = dir.path().join("does-not-exist");
        assert_eq!(
            resolve_arw(&root, "DSC00001").unwrap_err().code,
            "SOURCE_ROOT_NOT_FOUND"
        );
    }

    #[test]
    fn rejects_symlink_pointing_outside_root() {
        let outside = TempDir::new("outside");
        let target = outside.path().join("secret.ARW");
        touch(&target);
        let root = TempDir::new("symroot");
        std::os::unix::fs::symlink(&target, root.path().join("DSC00005.ARW")).expect("symlink");
        assert_eq!(
            resolve_arw(root.path(), "DSC00005").unwrap_err().code,
            "ARW_OUTSIDE_SOURCE_ROOT"
        );
    }

    #[test]
    fn source_root_config() {
        let dir = TempDir::new("config");
        assert_eq!(
            read_source_root(dir.path()).unwrap_err().code,
            "SOURCE_ROOT_NOT_CONFIGURED"
        );
        fs::write(dir.path().join("config.json"), "{not json").unwrap();
        assert_eq!(
            read_source_root(dir.path()).unwrap_err().code,
            "CONFIG_INVALID"
        );
        fs::write(dir.path().join("config.json"), r#"{"config_version":1}"#).unwrap();
        assert_eq!(
            read_source_root(dir.path()).unwrap_err().code,
            "SOURCE_ROOT_NOT_CONFIGURED"
        );
        fs::write(
            dir.path().join("config.json"),
            r#"{"source_root":"relative/path"}"#,
        )
        .unwrap();
        assert_eq!(
            read_source_root(dir.path()).unwrap_err().code,
            "SOURCE_ROOT_NOT_CONFIGURED"
        );
        fs::write(
            dir.path().join("config.json"),
            r#"{"config_version":1,"source_root":"/Volumes/X"}"#,
        )
        .unwrap();
        assert_eq!(
            read_source_root(dir.path()).unwrap(),
            PathBuf::from("/Volumes/X")
        );
    }

    #[test]
    fn develop_mode_deserializes_only_known_values() {
        assert_eq!(
            serde_json::from_str::<DevelopMode>("\"preview\"").unwrap(),
            DevelopMode::Preview
        );
        assert_eq!(
            serde_json::from_str::<DevelopMode>("\"full\"").unwrap(),
            DevelopMode::Full
        );
        assert!(serde_json::from_str::<DevelopMode>("\"preview\\\",\\\"x\\\":\\\"1\"").is_err());
        assert!(serde_json::from_str::<DevelopMode>("\"Preview\"").is_err());
    }

    #[test]
    fn base_request_respects_cli_contract() {
        let input = Path::new("/abs/DSC00001.ARW");
        let preview = base_request(input, DevelopMode::Preview);
        assert_eq!(preview.get("max_edge"), Some(&json!(2400)));
        assert_eq!(preview.get("decoder_version"), Some(&json!("9")));
        assert_eq!(
            preview.get("settings"),
            Some(&json!({"profile": "neutral"}))
        );
        let full = base_request(input, DevelopMode::Full);
        assert!(
            full.get("max_edge").is_none(),
            "full に max_edge を付けると CLI がエラーにする"
        );
        assert!(full.get("jpeg_quality").is_none());
    }

    #[test]
    fn cache_key_is_stable_and_sensitive() {
        let version = CliVersion {
            fingerprint: "{\"cli_version\":\"0.1.0\"}".into(),
            cli_version: "0.1.0".into(),
        };
        let request = base_request(Path::new("/abs/DSC00001.ARW"), DevelopMode::Preview);
        let key = cache_key(&request, &version, 100, 5);
        assert_eq!(key.len(), 16);
        assert_eq!(key, cache_key(&request, &version, 100, 5));
        assert_ne!(key, cache_key(&request, &version, 101, 5));
        assert_ne!(key, cache_key(&request, &version, 100, 6));
        let other_version = CliVersion {
            fingerprint: "{\"cli_version\":\"0.2.0\"}".into(),
            cli_version: "0.2.0".into(),
        };
        assert_ne!(key, cache_key(&request, &other_version, 100, 5));
        let full = base_request(Path::new("/abs/DSC00001.ARW"), DevelopMode::Full);
        assert_ne!(key, cache_key(&full, &version, 100, 5));
        let other_path = base_request(Path::new("/other/DSC00001.ARW"), DevelopMode::Preview);
        assert_ne!(key, cache_key(&other_path, &version, 100, 5));
    }

    #[test]
    fn sidecar_stdin_is_closed() {
        // stdin を閉じないと cat は終わらずウォッチドッグまで待つことになる。
        let started = Instant::now();
        let output = run_sidecar(
            Path::new("/bin/cat"),
            &[],
            Some(b"{\"ok\":true}"),
            Duration::from_secs(10),
        )
        .unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, "{\"ok\":true}");
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn sidecar_watchdog_kills_hung_process() {
        let started = Instant::now();
        let error = run_sidecar(
            Path::new("/bin/sleep"),
            &["30"],
            None,
            Duration::from_millis(300),
        )
        .unwrap_err();
        assert_eq!(error.code, "SIDECAR_TIMEOUT");
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn sidecar_spawn_failure() {
        let error = run_sidecar(
            Path::new("/nonexistent/geoframe-ci-develop"),
            &[],
            None,
            Duration::from_secs(1),
        )
        .unwrap_err();
        assert_eq!(error.code, "SIDECAR_SPAWN_FAILED");
    }

    #[test]
    fn develop_slot_is_exclusive_and_released() {
        let flag = AtomicBool::new(false);
        {
            let _slot = DevelopSlot::try_acquire(&flag).expect("空いているので取れる");
            assert!(
                DevelopSlot::try_acquire(&flag).is_none(),
                "使用中は取れない（BUSY）"
            );
        }
        assert!(
            DevelopSlot::try_acquire(&flag).is_some(),
            "drop で解放される"
        );

        // エラーで早期 return しても解放される。
        fn fails(flag: &AtomicBool) -> Result<(), CoreImageError> {
            let _slot = DevelopSlot::try_acquire(flag).expect("slot");
            Err(CoreImageError::new("X", "x"))
        }
        assert!(fails(&flag).is_err());
        assert!(!flag.load(Ordering::Acquire));

        // panic の巻き戻しでも解放される。
        let result = std::panic::catch_unwind(|| {
            let _slot = DevelopSlot::try_acquire(&flag).expect("slot");
            panic!("boom");
        });
        assert!(result.is_err());
        assert!(DevelopSlot::try_acquire(&flag).is_some());
    }

    #[test]
    fn provenance_temp_names_are_unique() {
        let names: std::collections::HashSet<String> =
            (0..1000).map(|_| provenance_temp_name("a.json")).collect();
        assert_eq!(names.len(), 1000);
        let threaded: Vec<String> = (0..8)
            .map(|_| thread::spawn(|| provenance_temp_name("a.json")))
            .map(|handle| handle.join().unwrap())
            .collect();
        let unique: std::collections::HashSet<&String> = threaded.iter().collect();
        assert_eq!(unique.len(), threaded.len());
        let name = provenance_temp_name("a.json");
        assert!(name.starts_with(".a.json.") && name.ends_with(".tmp"));
    }

    #[test]
    fn cleanup_removes_only_stale_dot_tmp_files() {
        let dir = TempDir::new("cleanup");
        let old = std::time::SystemTime::now() - Duration::from_secs(11 * 60);
        let make = |name: &str, stale: bool| {
            let path = dir.path().join(name);
            touch(&path);
            if stale {
                fs::File::options()
                    .write(true)
                    .open(&path)
                    .unwrap()
                    .set_modified(old)
                    .unwrap();
            }
            path
        };
        let stale_tmp = make(".x.jpg.ABC.tmp", true);
        let fresh_tmp = make(".y.jpg.DEF.tmp", false);
        let stale_jpg = make("DSC00001_preview_0123.jpg", true);
        let stale_no_dot = make("z.tmp", true);
        // 外を指す古いシンボリックリンクは、リンク先もリンク自体も消さない。
        let outside = TempDir::new("cleanup-outside");
        let target = outside.path().join(".target.tmp");
        touch(&target);
        fs::File::options()
            .write(true)
            .open(&target)
            .unwrap()
            .set_modified(old)
            .unwrap();
        let link = dir.path().join(".link.tmp");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        // 先頭ドット + .tmp のディレクトリも対象外。
        let tmp_dir = dir.path().join(".dir.tmp");
        fs::create_dir(&tmp_dir).unwrap();

        cleanup_stale_temps(dir.path(), STALE_TEMP_AGE);

        assert!(!stale_tmp.exists(), "古い一時ファイルは消える");
        assert!(
            fresh_tmp.exists(),
            "新しい一時ファイル（現像中かもしれない）は残る"
        );
        assert!(stale_jpg.exists());
        assert!(stale_no_dot.exists());
        assert!(fs::symlink_metadata(&link).is_ok());
        assert!(target.exists());
        assert!(tmp_dir.is_dir());
    }

    /// `scripts/build-coreimage-cli.sh` が置いた externalBin。
    fn built_sidecar() -> Option<PathBuf> {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("binaries");
        fs::read_dir(dir)
            .ok()?
            .flatten()
            .map(|entry| entry.path())
            .find(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.starts_with("geoframe-ci-develop-"))
            })
    }

    const REAL_ROOT: &str = "/Volumes/Backup/100MSDCF";

    /// 実 ARW での結合テスト（Tauri の develop_coreimage と同じ `develop` を通す）。
    /// 実ファイルか sidecar が無ければ skip。出力は一時ディレクトリの偽 app_data_dir へ書き、
    /// 本物の `~/Library/Application Support/com.geoframe.rawassist/` には触れない。
    #[test]
    fn develops_real_arw_when_available() {
        let Some(sidecar) = built_sidecar() else {
            eprintln!("skip: src-tauri/binaries/geoframe-ci-develop-* がありません");
            return;
        };
        let cases = [("DSC04430", true), ("DSC04553", false)];
        if cases
            .iter()
            .any(|(id, _)| !Path::new(REAL_ROOT).join(format!("{id}.ARW")).is_file())
        {
            eprintln!("skip: {REAL_ROOT} の実 ARW がありません");
            return;
        }
        let app_data = TempDir::new("appdata");
        fs::write(
            app_data.path().join("config.json"),
            json!({"config_version": 1, "source_root": REAL_ROOT}).to_string(),
        )
        .unwrap();

        for (capture_id, landscape) in cases {
            let input = Path::new(REAL_ROOT).join(format!("{capture_id}.ARW"));
            let before = fs::metadata(&input).unwrap();

            let first = develop(
                app_data.path(),
                &sidecar,
                capture_id,
                DevelopMode::Preview,
                DEVELOP_TIMEOUT,
            )
            .unwrap_or_else(|error| panic!("{capture_id}: {error:?}"));
            assert!(!first.cache_hit);
            assert_eq!(first.decoder_version, "9");
            let (long, short) = if landscape {
                (first.width, first.height)
            } else {
                (first.height, first.width)
            };
            assert_eq!((long, short), (2400, 1600), "{capture_id} の向き・寸法");
            let output = PathBuf::from(&first.output_path);
            let output_dir = app_data
                .path()
                .join(OUTPUT_DIR_NAME)
                .canonicalize()
                .unwrap();
            assert_eq!(output.parent(), Some(output_dir.as_path()));
            assert!(output.is_file());
            assert!(output.with_extension("json").is_file(), "来歴 JSON");
            assert!(first.elapsed_ms.get("total").is_some());

            let second = develop(
                app_data.path(),
                &sidecar,
                capture_id,
                DevelopMode::Preview,
                DEVELOP_TIMEOUT,
            )
            .unwrap();
            assert!(second.cache_hit, "2 回目はキャッシュヒット");
            assert_eq!(second.output_path, first.output_path);
            assert_eq!((second.width, second.height), (first.width, first.height));

            // 原本不変（サイズ・mtime）。
            let after = fs::metadata(&input).unwrap();
            assert_eq!(before.len(), after.len());
            assert_eq!(before.modified().unwrap(), after.modified().unwrap());
        }

        // 存在しない ID は CLI を呼ぶ前に落ちる。
        let error = develop(
            app_data.path(),
            &sidecar,
            "DSC00000",
            DevelopMode::Preview,
            DEVELOP_TIMEOUT,
        )
        .unwrap_err();
        assert_eq!(error.code, "ARW_NOT_FOUND");
    }
}
