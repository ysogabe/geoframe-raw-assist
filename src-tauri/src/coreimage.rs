//! Core Image プレビュー現像（Issue #72 / #73。設計書
//! `docs/design/coreimage-preview-adjustments.md` §2・§4.3）。
//!
//! Python サーバ / `.venv` / localhost HTTP を一切経由せず、Rust から Swift CLI
//! `geoframe-ci-develop`（`native/coreimage-cli`、Tauri の externalBin）を子プロセスとして
//! 直接呼ぶ。CLI の入出力契約は `native/coreimage-cli/README.md` と設計書 §4.1・§4.2。
//!
//! - プレビュー・原寸とも Core Image。エラーはフロントで表示する。
//! - `config.json`（Python の `local_raw/config.py` と同じファイル）は `source_root` を
//!   **読むだけ**。書き込みは Python 側の責務のまま。
//! - capture_id → ARW の解決は `local_raw/source_files.py` と同じ規則の再実装。
//! - 出力先は `<app_data_dir>/coreimage/preview/` 配下で Rust が決める（フロントからパスは
//!   受けない）。元の ARW / JPG と、Python 側のキャッシュ・書き出し先には書かない。
//! - Adjustments は `crate::adjustments::normalize_adjustments` で検証・正規化してから
//!   CLI へ全量を渡す。写像（Adjustments → CIRAWFilter / 後段）はすべて CLI 側。

use std::collections::HashMap;
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha1::Sha1;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};

use crate::adjustments::normalize_adjustments;

/// externalBin の名前（tauri-build が実行ファイルの隣へ triple 無しの名前でコピーする）。
const SIDECAR_NAME: &str = "geoframe-ci-develop";

/// 出力ディレクトリ名（`<app_data_dir>/coreimage/`）。tauri.conf.json の
/// `assetProtocol.scope`（`$APPDATA/coreimage/**`）と一致させること。モードごとに
/// サブディレクトリ（`preview/`、`full/`）を切る。
const OUTPUT_DIR_NAME: &str = "coreimage";

/// 現像 1 回のウォッチドッグ。超えたら子プロセスを kill してエラーにする。
/// スロットの待ち時間は含めない（子プロセスの起動から数える）。
const DEVELOP_TIMEOUT: Duration = Duration::from_secs(60);
/// `--version` は即答するはずなので短めに切る。
const VERSION_TIMEOUT: Duration = Duration::from_secs(10);

/// アプリから CLI へ渡す固定値（設計書 §2.2）。decoder_version は OS 更新で既定値が
/// 変わりうるので明示する。profile は `apple_default`（neutral v9 preview は白点が出る）。
/// CLI 自身の既定（neutral）は変えない。
const DECODER_VERSION: &str = "9";
const PROFILE: &str = "apple_default";
const LENS_CORRECTION: bool = true;
const PREVIEW_MAX_EDGE: u32 = 2400;
const FORMAT: &str = "jpeg";

/// これより古い `.tmp` は放置された一時ファイルとみなして消す。現像 1 回のウォッチドッグ
/// （60 秒）より十分長く取り、走行中の現像の一時ファイルには触れない。
const STALE_TEMP_AGE: Duration = Duration::from_secs(10 * 60);

/// キャッシュ名ハッシュの入力形式の版。ハッシュ入力の組み立てや、Rust 側の
/// Adjustments 正規化規則（クランプ・float 化）を変えたら上げる。
/// 1: 試験経路（#65）。2: adjustments を含む request 全量の正規形（#72）。
/// 3: CLI 実行ファイルの SHA-256 を足した（#72 レビュー）。
const CACHE_KEY_SCHEMA: u32 = 3;

/// キャッシュ上限（設計書 §2.3、オーナー判断 8）。件数は `.jpg` + `.json` の組を 1 件と数える。
/// 超えたら mtime の古い組から消して上限の 80% まで落とす。
const CACHE_MAX_ENTRIES: usize = 600;
const CACHE_MAX_BYTES: u64 = 1_500_000_000;

/// CLI の実行待ちに並べる件数（走行中の 1 本を除く）。超えたら BUSY（設計書 §2.4）。
const MAX_WAITING: usize = 2;

/// フロントへ返すエラー。CLI の `error.code` / `message` はそのまま詰め替える。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CoreImageError {
    pub code: String,
    pub message: String,
}

impl CoreImageError {
    pub(crate) fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }
}

/// 現像モード。自由文字列を CLI の JSON へ流さないよう enum で受ける。
/// full は max_edge を送らず、CLI の原寸出力を使う。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DevelopMode {
    Preview,
    Full,
}

impl DevelopMode {
    /// CLI の `mode` 値であり、出力サブディレクトリ名でもある。
    fn as_str(self) -> &'static str {
        match self {
            DevelopMode::Preview => "preview",
            DevelopMode::Full => "full",
        }
    }
}

/// `develop_preview_coreimage` の応答（設計書 §4.3。フロントの `CoreImagePreviewResult` と
/// 同名フィールド）。
#[derive(Debug, Clone, Serialize)]
pub struct CoreImagePreviewResult {
    pub capture_id: String,
    /// 出力 JPEG の絶対パス（フロントは `convertFileSrc` で表示 URL にする）。
    pub output_path: String,
    /// 来歴 JSON（CLI 応答の全文）の絶対パス。
    pub provenance_path: String,
    pub width: u64,
    pub height: u64,
    /// CLI の `elapsed_ms`（`decode` / `render_encode` / `total`）。キャッシュヒット時は
    /// 最初に現像したときの値。
    pub elapsed_ms: Value,
    pub cache_hit: bool,
    pub preview: bool,
    /// Rust の APP1 後処理に成功した場合のみ true。
    pub exif_inherited: bool,
    /// キャッシュ鍵（hash16）。ファイル名にも入る。
    pub params_hash: String,
    pub decoder_version: String,
    pub cli_version: String,
    pub macos_version: String,
    /// Rust が正規化（クランプ・float 化）した Adjustments 全量。
    pub adjustments: Value,
    /// CLI の `applied` そのまま。
    pub applied: Value,
    /// CLI 0.3.0 のトップレベル `adjustments_applied`（defaults / post / ignored / guards。0.6.0 から
    /// highlight_recovery の実効値 `highlight_recovery` が増え、`ignored` は空配列）
    /// そのまま。CLI が返さなければ（0.2.0 など）null。§4.3 の `applied` のコメントと §4.2 の
    /// 食い違いは §4.2 を正とし、このフィールドを足す（メイン決定 2026-10-20）。
    pub adjustments_applied: Value,
    /// CLI の `source`（lens_model / iso / camera_model 等）。
    pub source: Value,
}

// ---------------------------------------------------------------------------
// 設定（source_root）の読み込み
// ---------------------------------------------------------------------------

/// `<app_data_dir>/config.json` の `source_root` を読む（書き込まない）。
pub fn read_source_root(app_data_dir: &Path) -> Result<PathBuf, CoreImageError> {
    probe::note_config_read();
    crate::source_folder::read_source_root(app_data_dir)
}

/// config.json の同一性の目印（Issue #94）。サイズ・mtime・inode の組。
///
/// mtime は APFS ではナノ秒精度だが、同じ瞬間に同じサイズで書き換わる場合や、Python 側が
/// 一時ファイル + rename で差し替える場合（inode が変わる）にも読み直すよう 3 つを組にする。
#[derive(Debug, Clone, PartialEq, Eq)]
struct ConfigStamp {
    len: u64,
    modified: Option<SystemTime>,
    ino: u64,
}

impl ConfigStamp {
    fn of(metadata: &fs::Metadata) -> Self {
        use std::os::unix::fs::MetadataExt;
        Self {
            len: metadata.len(),
            modified: metadata.modified().ok(),
            ino: metadata.ino(),
        }
    }
}

/// config キャッシュに置く件数の上限（キーは config.json のパス。アプリでは 1 件しか使わない。
/// テストは並列に別々の app_data_dir を使うので、1 枠だと互いに上書きし合う）。
const SOURCE_ROOT_CACHE_MAX: usize = 64;

/// `read_source_root` の結果を config.json の [`ConfigStamp`] ごとに覚える版（Issue #94）。
///
/// キャッシュヒット時は config.json を stat するだけで、読み直し・JSON 解析をしない。
/// 目印が変わったら（撮影フォルダの切り替えで Python が書き換えた等）読み直すので、古い root を
/// 使い続けない。stat に失敗したとき・目印が違うときは `read_source_root` をそのまま呼ぶ
/// （エラーの判定は常に `read_source_root` に任せ、エラー契約をここで作らない）。成功だけ覚える。
fn read_source_root_cached(app_data_dir: &Path) -> Result<PathBuf, CoreImageError> {
    static CACHE: OnceLock<Mutex<HashMap<PathBuf, (ConfigStamp, PathBuf)>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let config_path = app_data_dir.join("config.json");
    let Ok(metadata) = fs::metadata(&config_path) else {
        return read_source_root(app_data_dir);
    };
    let stamp = ConfigStamp::of(&metadata);
    if let Some((cached_stamp, root)) = cache
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .get(&config_path)
    {
        if *cached_stamp == stamp {
            return Ok(root.clone());
        }
    }
    let root = read_source_root(app_data_dir)?;
    // 読む直前の stat の目印で覚える。stat と読み込みの間に書き換わった場合は、覚えた目印が
    // 次回の stat と食い違うので次回読み直される（古い root を覚え続けることはない）。
    let mut guard = cache
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if guard.len() >= SOURCE_ROOT_CACHE_MAX && !guard.contains_key(&config_path) {
        guard.clear();
    }
    guard.insert(config_path, (stamp, root.clone()));
    Ok(root)
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

/// 解決メモ（`(canonical root, capture_id)` → 解決済みパス）の件数上限。超えたら全消去する。
/// 1 件は数百バイトなので、撮影フォルダ数枚ぶん（数千枚）を覚えても数 MB に収まる。
const ARW_MEMO_MAX: usize = 4096;

/// テストから「撮影フォルダの列挙（`read_dir`）が何回走ったか」「config.json を何回読んだか」を
/// 数えるフック。スレッドローカルなのは、並列に走る他のテストの列挙・読み込みを数えないため
/// （解決はすべて呼び出しスレッドで同期的に行われる）。本番ビルドでは何もしない。
#[cfg(test)]
mod probe {
    use std::cell::Cell;

    thread_local! {
        pub(super) static READ_DIR_CALLS: Cell<usize> = const { Cell::new(0) };
        pub(super) static CONFIG_READS: Cell<usize> = const { Cell::new(0) };
    }

    pub(super) fn note_read_dir() {
        READ_DIR_CALLS.with(|calls| calls.set(calls.get() + 1));
    }

    pub(super) fn note_config_read() {
        CONFIG_READS.with(|calls| calls.set(calls.get() + 1));
    }
}

#[cfg(not(test))]
mod probe {
    pub(super) fn note_read_dir() {}
    pub(super) fn note_config_read() {}
}

fn arw_memo() -> MutexGuard<'static, HashMap<(PathBuf, String), PathBuf>> {
    static MEMO: OnceLock<Mutex<HashMap<(PathBuf, String), PathBuf>>> = OnceLock::new();
    MEMO.get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// メモの解決結果がまだ使えるか。消えた・シンボリックリンクに差し替わった（canonicalize の結果が
/// 自分自身でなくなる）・フォルダ外になった・通常ファイルでなくなった、のどれかなら使えない。
fn memo_still_valid(root: &Path, memo: &Path) -> bool {
    match memo.canonicalize() {
        Ok(canonical) => canonical == memo && memo.parent() == Some(root) && memo.is_file(),
        Err(_) => false,
    }
}

/// `root` 直下から `capture_id` の ARW を実エントリ名で解決し、canonicalize したパスを返す。
///
/// シンボリックリンクでフォルダ外を指すもの（解決後の親が root と一致しないもの）は拒否する。
///
/// Issue #94: 撮影フォルダ全体の列挙はフォルダが大きい・外付けボリュームだと遅く、キャッシュに
/// 当たる現像でも毎回走っていた。そこで成功した解決を `(canonical root, capture_id)` でメモし、
/// 2 回目以降はメモのパスを canonicalize / stat して確かめるだけで返す（列挙しない）。
///
/// - ID の形式検査と root の canonicalize / ディレクトリ検査はメモより前に毎回行う
///   （INVALID_CAPTURE_ID / SOURCE_ROOT_NOT_FOUND の契約はメモの有無で変わらない）。
/// - メモが使えなければ（[`memo_still_valid`]）消して、従来の列挙経路で解決し直す。エラーは
///   すべて列挙経路が出すので、ARW_NOT_FOUND / ARW_OUTSIDE_SOURCE_ROOT の条件も従来どおり。
/// - 返すのは前回返したパスそのもの（実エントリ名・拡張子の大小を含む）。キャッシュ鍵がパスを
///   含むので、作り直すと不要なキャッシュミスになる。`<ID>.ARW` を直接 stat する方式は、
///   大小文字を区別しないボリュームで実エントリ名（`.arw` 等）が得られないので採らない。
/// - エラーは覚えない（ファイルが後から置かれたら次回見つかる）。
/// - 受け入れたずれ: 大小文字を区別するボリュームで `<ID>.arw` をメモした後に `<ID>.ARW` が
///   足されても、メモが使える限り `pick_arw_entry` の「正規表記優先」は反映されない（メモの
///   ファイル自体は存在・フォルダ内・通常ファイルであることを確かめている）。APFS の既定
///   （大小文字を区別しない）では 2 つは共存できない。
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
    let key = (root, capture_id.to_string());
    let memo = arw_memo().get(&key).cloned();
    if let Some(memo) = memo {
        if memo_still_valid(&key.0, &memo) {
            return Ok(memo);
        }
        arw_memo().remove(&key);
    }
    let resolved = enumerate_arw(&key.0, capture_id)?;
    let mut memo = arw_memo();
    if memo.len() >= ARW_MEMO_MAX && !memo.contains_key(&key) {
        memo.clear();
    }
    memo.insert(key, resolved.clone());
    Ok(resolved)
}

/// 従来の解決経路。canonicalize 済みのディレクトリ `root` を列挙して `capture_id` の ARW を探す。
fn enumerate_arw(root: &Path, capture_id: &str) -> Result<PathBuf, CoreImageError> {
    probe::note_read_dir();
    let entries = fs::read_dir(root).map_err(|error| {
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
    if resolved.parent() != Some(root) {
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
/// - stdin への書き込みに失敗しても（CLI がリクエストを読み切る前にエラーで終わった EPIPE 等）
///   すぐには kill せず、同じウォッチドッグで終了を待つ。stdout に CLI の構造化エラー
///   （`{"ok":false,"error":{"code",...}}`）があればそれを返し、無い・読めないときだけ
///   `SIDECAR_IO_FAILED` にする（CLI が出した本当の原因を捨てないため）。
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

    let mut write_error = None;
    if let Some(bytes) = stdin_bytes {
        // take() で取り出してスコープ末尾で drop する = EOF を送る。
        if let Some(mut stdin) = child.stdin.take() {
            if let Err(error) = stdin.write_all(bytes) {
                // ここでは kill しない。CLI が先にエラー応答を出して終わった可能性があるので、
                // 下の終了待ち（ウォッチドッグ付き）に任せてから stdout を確かめる。
                write_error = Some(error);
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
    let output = SidecarOutput {
        status,
        stdout,
        stderr,
    };
    if let Some(error) = write_error {
        // リクエストを渡し切れていないので、成功応答は信用しない。構造化エラーだけ採る。
        return match parse_response(&output) {
            Ok(response) if is_cli_error_response(&response) => Ok(output),
            _ => Err(CoreImageError::new(
                "SIDECAR_IO_FAILED",
                format!("CLI へのリクエスト送信に失敗しました: {error}"),
            )),
        };
    }
    Ok(output)
}

/// `{"ok":false,"error":{"code":"...","message":"..."}}` の形か（`cli_error` がそのまま詰め替えられる形）。
fn is_cli_error_response(response: &Value) -> bool {
    let error = response.get("error");
    response.get("ok") == Some(&Value::Bool(false))
        && error
            .and_then(|e| e.get("code"))
            .and_then(Value::as_str)
            .is_some()
        && error
            .and_then(|e| e.get("message"))
            .and_then(Value::as_str)
            .is_some()
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
// バッチセッション（CLI `--batch`、Issue #82）
// ---------------------------------------------------------------------------

/// CLI を `--batch` で常駐させ、JSON Lines で複数のリクエストを流すセッション。
///
/// 今は呼び出し側が無い（PSO・ブラケット合成で使う予定）ので、未使用の警告だけを抑えている。
#[cfg_attr(not(test), allow(dead_code))]
mod batch {
    use std::collections::VecDeque;
    use std::io::{BufRead, BufReader, Write};
    use std::path::Path;
    use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    use serde_json::Value;

    use super::{is_cli_error_response, CoreImageError};

    /// stderr の末尾として保持する行数（エラーメッセージに添える）。
    const STDERR_TAIL_LINES: usize = 20;
    /// Drop で stdin を閉じてから自然終了を待つ時間。過ぎたら kill する。
    const DROP_GRACE: Duration = Duration::from_millis(500);
    /// 応答の前に子が終わったとき、stderr の末尾を読み切るまで待つ上限。
    const STDERR_SETTLE: Duration = Duration::from_millis(500);

    /// `program --batch` を起動して保持する。
    ///
    /// - 1 リクエスト = 1 行（改行を含まないコンパクトな JSON）を書き、応答 1 行を待つ。
    ///   待ち時間は **1 リクエストごと**に `timeout`。過ぎたら子プロセスを kill し、
    ///   セッションは使えなくなる（以後のリクエストは `SIDECAR_SESSION_CLOSED`）。
    /// - 応答の `sequence`（CLI が受け付けた順の番号、1 始まり）が送った件数と一致することを確かめる。
    ///   食い違い・JSON でない応答・`sequence` 欠落・子の終了は、対応が取れなくなったとみなして
    ///   kill し、使えない状態にする。
    /// - CLI の失敗応答（`{"ok":false,...}`）はその 1 件の `Err` として返し、セッションは使い続けられる。
    /// - stdout / stderr はそれぞれ専用スレッドで読む（stderr は CLI が 1 件ごとにログを書くので、
    ///   読まないとパイプが詰まって子が止まる）。stderr は末尾だけ保持する。
    /// - 終了は `finish`（stdin を閉じて wait）。呼ばずに drop しても、stdin を閉じて短く待ち、
    ///   終わらなければ kill して回収する。
    pub(super) struct BatchSession {
        child: Option<Child>,
        stdin: Option<ChildStdin>,
        responses: Receiver<String>,
        stderr_tail: Arc<Mutex<VecDeque<String>>>,
        /// stderr を EOF まで読み終えたか（子の終了後、末尾を取りこぼさないよう待つため）。
        stderr_done: Arc<AtomicBool>,
        sent: u64,
        timeout: Duration,
        usable: bool,
    }

    impl BatchSession {
        /// `program --batch` を起動する。`timeout` は 1 リクエストごとの待ち時間（`finish` の待ちにも使う）。
        pub(super) fn start(program: &Path, timeout: Duration) -> Result<Self, CoreImageError> {
            let mut child = Command::new(program)
                .arg("--batch")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .map_err(|error| {
                    CoreImageError::new(
                        "SIDECAR_SPAWN_FAILED",
                        format!("{} を起動できません: {error}", program.display()),
                    )
                })?;
            let stdin = child.stdin.take();
            let (sender, responses) = mpsc::channel();
            if let Some(stdout) = child.stdout.take() {
                thread::spawn(move || {
                    for line in BufReader::new(stdout).lines() {
                        let Ok(line) = line else { break };
                        if sender.send(line).is_err() {
                            break;
                        }
                    }
                });
            }
            let stderr_tail = Arc::new(Mutex::new(VecDeque::new()));
            let stderr_done = Arc::new(AtomicBool::new(false));
            if let Some(stderr) = child.stderr.take() {
                let tail = Arc::clone(&stderr_tail);
                let done = Arc::clone(&stderr_done);
                thread::spawn(move || {
                    for line in BufReader::new(stderr).lines() {
                        let Ok(line) = line else { break };
                        let mut tail = tail.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                        if tail.len() == STDERR_TAIL_LINES {
                            tail.pop_front();
                        }
                        tail.push_back(line);
                    }
                    done.store(true, Ordering::SeqCst);
                });
            } else {
                stderr_done.store(true, Ordering::SeqCst);
            }
            Ok(Self {
                child: Some(child),
                stdin,
                responses,
                stderr_tail,
                stderr_done,
                sent: 0,
                timeout,
                usable: true,
            })
        }

        /// まだリクエストを流せるか（タイムアウト・対応の食い違い・子の終了の後は false）。
        pub(super) fn is_usable(&self) -> bool {
            self.usable
        }

        /// これまでに送ったリクエストの件数（= 最後に期待した `sequence`）。
        pub(super) fn sent(&self) -> u64 {
            self.sent
        }

        /// 子プロセスの pid（テストで後片付けを確かめる用）。
        pub(super) fn pid(&self) -> Option<u32> {
            self.child.as_ref().map(Child::id)
        }

        /// 1 件を送り、応答を待つ。成功応答（`ok: true`）は JSON のまま返す。
        pub(super) fn request(&mut self, request: &Value) -> Result<Value, CoreImageError> {
            if !self.usable {
                return Err(CoreImageError::new(
                    "SIDECAR_SESSION_CLOSED",
                    "Core Image CLI のバッチセッションは既に使えません",
                ));
            }
            // serde_json の既定の出力は 1 行（文字列中の改行はエスケープされる）。
            let mut line = serde_json::to_vec(request).map_err(|error| {
                CoreImageError::new(
                    "SIDECAR_IO_FAILED",
                    format!("リクエストを JSON にできません: {error}"),
                )
            })?;
            line.push(b'\n');
            let expected = self.sent + 1;
            let written = match self.stdin.as_mut() {
                Some(stdin) => stdin.write_all(&line).and_then(|()| stdin.flush()),
                None => Err(std::io::Error::other("stdin is closed")),
            };
            if let Err(error) = written {
                return Err(self.abort(
                    "SIDECAR_IO_FAILED",
                    format!("CLI へのリクエスト送信に失敗しました: {error}"),
                ));
            }
            self.sent = expected;

            let response_line = match self.responses.recv_timeout(self.timeout) {
                Ok(line) => line,
                Err(RecvTimeoutError::Timeout) => {
                    return Err(self.abort(
                        "SIDECAR_TIMEOUT",
                        format!(
                            "Core Image CLI が {} 秒以内に応答しなかったため中断しました",
                            self.timeout.as_secs_f32()
                        ),
                    ));
                }
                Err(RecvTimeoutError::Disconnected) => {
                    let message = format!(
                        "Core Image CLI が応答の前に終了しました: {}",
                        self.stderr_summary()
                    );
                    return Err(self.abort("SIDECAR_BAD_RESPONSE", message));
                }
            };
            let response = match serde_json::from_str::<Value>(&response_line) {
                Ok(value) => value,
                Err(_) => {
                    let message = format!(
                        "Core Image CLI の応答を読めません: {}",
                        self.stderr_summary()
                    );
                    return Err(self.abort("SIDECAR_BAD_RESPONSE", message));
                }
            };
            let sequence = response.get("sequence").and_then(Value::as_u64);
            if sequence != Some(expected) {
                return Err(self.abort(
                    "SIDECAR_BAD_RESPONSE",
                    format!(
                        "Core Image CLI の応答の sequence が食い違いました（期待 {expected}、実際 {}）",
                        sequence.map_or_else(|| "なし".to_string(), |n| n.to_string())
                    ),
                ));
            }
            if response.get("ok") == Some(&Value::Bool(true)) {
                return Ok(response);
            }
            if is_cli_error_response(&response) {
                let error = &response["error"];
                return Err(CoreImageError::new(
                    error["code"].as_str().unwrap_or_default(),
                    error["message"].as_str().unwrap_or_default(),
                ));
            }
            Err(CoreImageError::new(
                "SIDECAR_BAD_RESPONSE",
                "Core Image CLI の応答が成功でも失敗の形でもありません",
            ))
        }

        /// stdin を閉じ（EOF）、`timeout` 以内の終了を待つ。過ぎたら kill して `SIDECAR_TIMEOUT`。
        pub(super) fn finish(mut self) -> Result<ExitStatus, CoreImageError> {
            self.usable = false;
            drop(self.stdin.take());
            let Some(mut child) = self.child.take() else {
                return Err(CoreImageError::new(
                    "SIDECAR_SESSION_CLOSED",
                    "Core Image CLI のバッチセッションは既に終了しています",
                ));
            };
            let deadline = Instant::now() + self.timeout;
            loop {
                match child.try_wait() {
                    Ok(Some(status)) => return Ok(status),
                    Ok(None) if Instant::now() >= deadline => {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Err(CoreImageError::new(
                            "SIDECAR_TIMEOUT",
                            format!(
                                "Core Image CLI が stdin を閉じてから {} 秒以内に終わらなかったため中断しました",
                                self.timeout.as_secs_f32()
                            ),
                        ));
                    }
                    Ok(None) => thread::sleep(Duration::from_millis(10)),
                    Err(error) => {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Err(CoreImageError::new(
                            "SIDECAR_IO_FAILED",
                            format!("CLI の終了待ちに失敗しました: {error}"),
                        ));
                    }
                }
            }
        }

        /// 子プロセスを kill して回収し、使えない状態にしてエラーを返す。
        fn abort(&mut self, code: &str, message: String) -> CoreImageError {
            self.usable = false;
            drop(self.stdin.take());
            if let Some(mut child) = self.child.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
            CoreImageError::new(code, message)
        }

        fn stderr_summary(&self) -> String {
            // 子の終了直後は stderr スレッドがまだ読み終えていないことがあるので、EOF まで少しだけ待つ。
            let deadline = Instant::now() + STDERR_SETTLE;
            while !self.stderr_done.load(Ordering::SeqCst) && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(5));
            }
            let tail = self
                .stderr_tail
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            tail.iter()
                .rev()
                .take(5)
                .cloned()
                .collect::<Vec<_>>()
                .join(" / ")
        }
    }

    impl Drop for BatchSession {
        fn drop(&mut self) {
            drop(self.stdin.take());
            let Some(mut child) = self.child.take() else {
                return;
            };
            let deadline = Instant::now() + DROP_GRACE;
            while Instant::now() < deadline {
                match child.try_wait() {
                    Ok(Some(_)) => return,
                    Ok(None) => thread::sleep(Duration::from_millis(10)),
                    Err(_) => break,
                }
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

// ---------------------------------------------------------------------------
// CLI バージョン（キャッシュキーの一部）
// ---------------------------------------------------------------------------

/// `--version` の応答（cli_version + macOS バージョンを含む）の正規形と、その中の 2 値、
/// およびサイドカー実行ファイルの SHA-256。
#[derive(Debug, Clone)]
struct CliVersion {
    fingerprint: String,
    cli_version: String,
    macos_version: String,
    /// 実行ファイル本体の SHA-256（16 進 64 桁）。cli_version を上げ忘れたまま CLI の実装
    /// （写像の定数など）が変わっても、キャッシュが古い出力を返さないようにする。
    binary_sha256: String,
}

/// サイドカー実行ファイルの SHA-256（16 進）。パスごとにプロセスで 1 回だけ計算して覚える
/// （.app 内のバイナリは起動中に変わらない前提。数 MB を現像のたびに読まない）。
/// 読めなければ `SIDECAR_IO_FAILED`（失敗は覚えない）。
fn sidecar_sha256(sidecar: &Path) -> Result<String, CoreImageError> {
    static CACHE: OnceLock<Mutex<HashMap<PathBuf, String>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Some(digest) = cache
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .get(sidecar)
    {
        return Ok(digest.clone());
    }
    let io_error = |error: std::io::Error| {
        CoreImageError::new(
            "SIDECAR_IO_FAILED",
            format!("{} を読めません: {error}", sidecar.display()),
        )
    };
    let mut file = fs::File::open(sidecar).map_err(io_error)?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher).map_err(io_error)?;
    let digest: String = hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    cache
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .insert(sidecar.to_path_buf(), digest.clone());
    Ok(digest)
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
    let text = |key: &str| {
        response
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string()
    };
    // 応答全文を正規形にして指紋にする（空白・キー順の差でキャッシュが割れないように）。
    let version = CliVersion {
        fingerprint: canonical_json(&response),
        cli_version: text("cli_version"),
        macos_version: text("macos_version"),
        binary_sha256: sidecar_sha256(sidecar)?,
    };
    let mut guard = CACHE
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    *guard = Some((sidecar.to_path_buf(), version.clone()));
    Ok(version)
}

// ---------------------------------------------------------------------------
// リクエストとキャッシュ鍵
// ---------------------------------------------------------------------------

/// CLI へのリクエスト（`output` 以外）。`output` はこれのハッシュから決まるので後で足す。
///
/// `adjustments` は `normalize_adjustments` 済みの全量（None なら CLI 既定の処理。
/// 0.2.0 互換の経路で、アプリのコマンドからは常に Some で呼ぶ）。
fn base_request(
    input: &Path,
    mode: DevelopMode,
    adjustments: Option<&Value>,
) -> serde_json::Map<String, Value> {
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
    request.insert(
        "settings".into(),
        json!({ "profile": PROFILE, "lens_correction": LENS_CORRECTION }),
    );
    if let Some(adjustments) = adjustments {
        request.insert("adjustments".into(), adjustments.clone());
    }
    request
}

/// キーを再帰的に辞書順へ並べた JSON 文字列。serde_json の `preserve_order` が依存経由で
/// 有効になっても（Map が挿入順になっても）ハッシュが変わらないようにする。
fn canonical_json(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let body: Vec<String> = keys
                .into_iter()
                .map(|key| {
                    format!(
                        "{}:{}",
                        Value::String(key.clone()),
                        canonical_json(&map[key.as_str()])
                    )
                })
                .collect();
            format!("{{{}}}", body.join(","))
        }
        Value::Array(items) => {
            let body: Vec<String> = items.iter().map(canonical_json).collect();
            format!("[{}]", body.join(","))
        }
        other => other.to_string(),
    }
}

/// キャッシュ名に使う安定ハッシュ（SHA-256 の先頭 16 桁）。
///
/// std の `DefaultHasher` は実行ごとに値が変わりうるので使わない。入力（設計書 §2.3）:
/// schema、リクエスト全量（output 以外。input は canonicalize 済みの絶対パス、settings、
/// 正規化後の adjustments）、CLI の `--version` 応答全文（cli_version + macos_version。
/// OS 更新で apple_default の既定値が変わる懸念を含める）、CLI 実行ファイルの SHA-256
/// （版を上げずに実装が変わった場合）、ARW の同一性（サイズ + mtime）。
/// パスを含めるのは、`capture_id` がカード／フォルダをまたいで衝突するため。
fn cache_key(
    request: &serde_json::Map<String, Value>,
    version: &CliVersion,
    size: u64,
    mtime_ns: u128,
) -> String {
    let mut request = request.clone();
    request.remove("output");
    let payload = json!({
        "schema": CACHE_KEY_SCHEMA,
        "request": Value::Object(request),
        "cli": version.fingerprint,
        "cli_binary_sha256": version.binary_sha256,
        "input_size": size,
        "input_mtime_ns": mtime_ns.to_string(),
    });
    let digest = Sha256::digest(canonical_json(&payload).as_bytes());
    digest
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// 結果を組み立てるための、応答以外の値。
struct ResultContext<'a> {
    capture_id: &'a str,
    output: &'a Path,
    provenance: &'a Path,
    key: &'a str,
    version: &'a CliVersion,
    adjustments: Option<&'a Value>,
}

fn result_from_response(
    response: &Value,
    context: &ResultContext<'_>,
    cache_hit: bool,
) -> Option<CoreImagePreviewResult> {
    let field = |key: &str| response.get(key).cloned().unwrap_or(Value::Null);
    Some(CoreImagePreviewResult {
        capture_id: context.capture_id.to_string(),
        output_path: context.output.to_string_lossy().into_owned(),
        provenance_path: context.provenance.to_string_lossy().into_owned(),
        width: response.get("width")?.as_u64()?,
        height: response.get("height")?.as_u64()?,
        elapsed_ms: field("elapsed_ms"),
        cache_hit,
        preview: response.get("mode").and_then(Value::as_str) != Some("full"),
        exif_inherited: response
            .get("exif_inherited")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        params_hash: context.key.to_string(),
        decoder_version: response
            .get("decoder_version")
            .and_then(Value::as_str)
            .unwrap_or(DECODER_VERSION)
            .to_string(),
        // 鍵に --version 全文が入っているので、キャッシュヒットでも現行の版と一致する。
        cli_version: context.version.cli_version.clone(),
        macos_version: context.version.macos_version.clone(),
        adjustments: context.adjustments.cloned().unwrap_or(Value::Null),
        applied: field("applied"),
        adjustments_applied: field("adjustments_applied"),
        source: field("source"),
    })
}

// ---------------------------------------------------------------------------
// 出力ディレクトリの掃除（一時ファイル・旧試験出力・LRU）
// ---------------------------------------------------------------------------

/// 来歴 JSON の一時ファイル名。pid だけだと同一プロセス内の並行書き込みで衝突しうるので、
/// プロセス内の通し番号も入れる。先頭ドット + `.tmp` は `cleanup_stale_temps` の掃除対象。
fn provenance_temp_name(file_name: &str) -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let sequence = COUNTER.fetch_add(1, Ordering::Relaxed);
    format!(".{file_name}.{}.{sequence}.tmp", std::process::id())
}

/// `dir` 直下の通常ファイル（シンボリックリンク・ディレクトリは除く）を
/// `(名前, パス, メタデータ)` で列挙する。非 UTF-8 名は対象パターンに当たり得ないので飛ばす。
fn regular_files(dir: &Path) -> Vec<(String, PathBuf, fs::Metadata)> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path).ok()?;
            metadata
                .file_type()
                .is_file()
                .then_some((name, path, metadata))
        })
        .collect()
}

/// `dir` 直下の、先頭ドットかつ `.tmp` で終わる通常ファイルのうち、最終更新から `max_age`
/// 以上経ったものを消す。CLI をタイムアウトで kill すると、CLI が output と同じディレクトリに
/// 作る `.<output名>.<UUID>.tmp` が残るため。シンボリックリンクは追わず（symlink_metadata）、
/// 削除の失敗は無視する（掃除は best effort）。
fn cleanup_stale_temps(dir: &Path, max_age: Duration) {
    let now = SystemTime::now();
    for (name, path, metadata) in regular_files(dir) {
        if !(name.starts_with('.') && name.ends_with(".tmp")) {
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

/// 試験経路（#65）が `coreimage/` 直下に残した `<ID>_<mode>_<hash16>.{jpg,json}`。
fn legacy_output_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"^DSC[0-9]{5}_(?:preview|full)_[0-9a-f]{16}\.(?:jpg|json)$")
            .expect("valid regex")
    })
}

/// 旧試験出力を `coreimage_dir` 直下から消す（再帰しない・通常ファイルのみ・best effort）。
/// アプリ自身が作った名前の形にだけ当てる（他のファイルには触れない）。消した数を返す。
fn cleanup_legacy_outputs(coreimage_dir: &Path) -> usize {
    let mut removed = 0;
    for (name, path, _) in regular_files(coreimage_dir) {
        if legacy_output_pattern().is_match(&name) && fs::remove_file(&path).is_ok() {
            removed += 1;
        }
    }
    // 試験経路が kill されたときの一時ファイルも同じ場所に残りうる。
    cleanup_stale_temps(coreimage_dir, STALE_TEMP_AGE);
    removed
}

/// 旧試験出力の掃除はプロセスにつき 1 回だけ（新経路の初回実行時）。
fn cleanup_legacy_outputs_once(coreimage_dir: &Path) {
    static DONE: AtomicBool = AtomicBool::new(false);
    if !DONE.swap(true, Ordering::AcqRel) {
        cleanup_legacy_outputs(coreimage_dir);
    }
}

/// 新経路のキャッシュ `<ID>_<hash16>.{jpg,json}`。グループ 1 がステム。
fn cache_entry_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"^(DSC[0-9]{5}_[0-9a-f]{16})\.(?:jpg|json)$").expect("valid regex")
    })
}

#[derive(Debug, Default)]
struct CacheEntry {
    files: Vec<PathBuf>,
    bytes: u64,
    newest: Option<SystemTime>,
}

/// キャッシュの上限付き LRU 削除（設計書 §2.3）。
///
/// `dir` 直下のキャッシュ名に当たる通常ファイルだけを `.jpg` + `.json` の組（ステム）で数え、
/// 件数 > `max_entries` または合計 > `max_bytes` なら、組の最新 mtime が古い順に組ごと消して
/// 両方を上限の 80% 以下まで落とす。`keep_stem`（いま書いた組）は消さない。シンボリック
/// リンク・ディレクトリ・名前の形が違うファイルには触れない。best effort（失敗は無視）。
/// 消した組の数を返す。
fn prune_cache(dir: &Path, max_entries: usize, max_bytes: u64, keep_stem: Option<&str>) -> usize {
    let mut entries: HashMap<String, CacheEntry> = HashMap::new();
    for (name, path, metadata) in regular_files(dir) {
        let Some(found) = cache_entry_pattern().captures(&name) else {
            continue;
        };
        let entry = entries.entry(found[1].to_string()).or_default();
        entry.files.push(path);
        entry.bytes = entry.bytes.saturating_add(metadata.len());
        let modified = metadata.modified().ok();
        entry.newest = entry.newest.max(modified);
    }
    let mut count = entries.len();
    let mut bytes: u64 = entries.values().map(|entry| entry.bytes).sum();
    if count <= max_entries && bytes <= max_bytes {
        return 0;
    }
    let target_entries = max_entries.saturating_mul(8) / 10;
    let target_bytes = max_bytes / 10 * 8;
    let mut ordered: Vec<(String, CacheEntry)> = entries.into_iter().collect();
    // mtime が取れないものは最古扱い（None < Some）。同時刻はステム順で決定的にする。
    ordered.sort_by(|(a_stem, a), (b_stem, b)| a.newest.cmp(&b.newest).then(a_stem.cmp(b_stem)));
    let mut removed = 0;
    for (stem, entry) in ordered {
        if count <= target_entries && bytes <= target_bytes {
            break;
        }
        if keep_stem == Some(stem.as_str()) {
            continue;
        }
        for file in &entry.files {
            let _ = fs::remove_file(file);
        }
        count -= 1;
        bytes = bytes.saturating_sub(entry.bytes);
        removed += 1;
    }
    removed
}

/// キャッシュヒットした組の mtime を今にする（LRU の「最近使った」）。best effort。
fn touch(path: &Path) {
    if let Ok(file) = fs::File::options().write(true).open(path) {
        let _ = file.set_modified(SystemTime::now());
    }
}

// ---------------------------------------------------------------------------
// 同時実行の制御（CLI は 1 本、待ちは MAX_WAITING 件まで）
// ---------------------------------------------------------------------------

/// CLI の実行枠。`Mutex<()>` で直列化し、走行中 + 待ちの件数を `pending` で数える。
/// 走行中 1 本 + 待ち `max_waiting` 件を超える呼び出しは待たずに BUSY にする。
pub(crate) struct DevelopSlot {
    lock: Mutex<()>,
    pending: AtomicUsize,
    max_waiting: usize,
}

/// 枠の RAII ガード。drop（正常終了・エラーの `?`・panic の巻き戻し）で必ず枠を返す。
/// フィールドは宣言順に drop される: 先に件数を返し、次にロックを外す。
pub(crate) struct SlotGuard<'a> {
    _ticket: PendingTicket<'a>,
    _lock: MutexGuard<'a, ()>,
}

/// `pending` を 1 つ持つ札（ロック待ちの間に panic しても件数を返す）。
struct PendingTicket<'a>(&'a AtomicUsize);

impl Drop for PendingTicket<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

impl DevelopSlot {
    pub(crate) const fn new(max_waiting: usize) -> Self {
        Self {
            lock: Mutex::new(()),
            pending: AtomicUsize::new(0),
            max_waiting,
        }
    }

    /// 枠を取る。空くまで待つ（待ちの上限を超えるなら BUSY）。
    pub(crate) fn acquire(&self) -> Result<SlotGuard<'_>, CoreImageError> {
        let limit = self.max_waiting.saturating_add(1);
        self.pending
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                (n < limit).then_some(n + 1)
            })
            .map_err(|_| {
                CoreImageError::new(
                    "BUSY",
                    "別の Core Image 現像を実行中です。終わってからもう一度試してください",
                )
            })?;
        let ticket = PendingTicket(&self.pending);
        let lock = self
            .lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        Ok(SlotGuard {
            _ticket: ticket,
            _lock: lock,
        })
    }

    #[cfg(test)]
    fn pending(&self) -> usize {
        self.pending.load(Ordering::Acquire)
    }
}

/// アプリ全体で 1 つの実行枠。
static DEVELOP_SLOT: DevelopSlot = DevelopSlot::new(MAX_WAITING);

// ---------------------------------------------------------------------------
// 現像本体
// ---------------------------------------------------------------------------

/// 来歴 JSON を一時ファイル経由で原子的に書く。
/// 一時ファイルを新規作成でだけ開く（既存のファイル・シンボリックリンクを辿って書かない）。
fn create_new_file(path: &Path) -> std::io::Result<fs::File> {
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
}

fn write_provenance(path: &Path, body: &str) -> Result<(), CoreImageError> {
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temp = path.with_file_name(provenance_temp_name(&file_name));
    let result = create_new_file(&temp)
        .and_then(|mut file| {
            use std::io::Write;
            file.write_all(body.as_bytes())
        })
        .and_then(|_| fs::rename(&temp, path));
    if let Err(error) = result {
        let _ = fs::remove_file(&temp);
        return Err(CoreImageError::new(
            "PROVENANCE_WRITE_FAILED",
            format!("来歴 JSON を書けません: {error}"),
        ));
    }
    Ok(())
}

fn output_dir_error(message: impl Into<String>) -> CoreImageError {
    CoreImageError::new("OUTPUT_DIR_FAILED", message)
}

/// `path` がシンボリックリンクでない実ディレクトリであることを確かめる。無ければ作る。
fn ensure_real_dir(path: &Path) -> Result<(), CoreImageError> {
    let check = |path: &Path| match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_dir() => Ok(true),
        Ok(_) => Err(output_dir_error(format!(
            "出力先がディレクトリではありません（シンボリックリンク等）: {}",
            path.display()
        ))),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(output_dir_error(format!(
            "出力先を確認できません: {}: {error}",
            path.display()
        ))),
    };
    if check(path)? {
        return Ok(());
    }
    match fs::create_dir(path) {
        // 並行して他の呼び出しが作った場合は、作られたものを確かめ直す。
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => check(path).map(|_| ()),
        Err(error) => Err(output_dir_error(format!(
            "出力先を作れません: {}: {error}",
            path.display()
        ))),
    }
}

/// `<app_data_dir>/coreimage/<mode>/` を作り、canonicalize した `(coreimage, mode)` の組を返す。
/// どちらかがシンボリックリンク等で外を指していたら拒否する（キャッシュの書き込み・
/// LRU 削除が `coreimage/<mode>/` の外へ出ないように）。
fn prepare_output_dirs(
    app_data_dir: &Path,
    mode: DevelopMode,
) -> Result<(PathBuf, PathBuf), CoreImageError> {
    let coreimage_dir = app_data_dir.join(OUTPUT_DIR_NAME);
    let mode_dir = coreimage_dir.join(mode.as_str());
    fs::create_dir_all(app_data_dir).map_err(|error| {
        output_dir_error(format!(
            "アプリのデータフォルダを作れません: {}: {error}",
            app_data_dir.display()
        ))
    })?;
    // create_dir_all はシンボリックリンクを辿って外に作ってしまうので、1 段ずつ
    // 「実ディレクトリか、無ければ作る」を確かめる。
    ensure_real_dir(&coreimage_dir)?;
    ensure_real_dir(&mode_dir)?;
    let canonical = |path: &Path| {
        path.canonicalize()
            .map_err(|error| output_dir_error(format!("出力先を解決できません: {error}")))
    };
    let app_data_dir = canonical(app_data_dir)?;
    let coreimage_dir = canonical(&coreimage_dir)?;
    let mode_dir = canonical(&mode_dir)?;
    if coreimage_dir.parent() != Some(app_data_dir.as_path())
        || mode_dir.parent() != Some(coreimage_dir.as_path())
    {
        return Err(output_dir_error(format!(
            "出力先がアプリのデータフォルダの外を指しています: {}",
            mode_dir.display()
        )));
    }
    Ok((coreimage_dir, mode_dir))
}

/// 画像と来歴の両方が通常ファイルで、来歴から結果を組み立てられればキャッシュヒット。
/// 画像だけなら来歴の書き込みが途中で落ちたとみなして現像し直す。
fn load_cached(context: &ResultContext<'_>) -> Option<CoreImagePreviewResult> {
    let is_regular = |path: &Path| {
        fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_file())
    };
    if !is_regular(context.output) || !is_regular(context.provenance) {
        return None;
    }
    let text = fs::read_to_string(context.provenance).ok()?;
    let saved = serde_json::from_str::<Value>(&text).ok()?;
    if saved.get("ok") != Some(&Value::Bool(true)) {
        return None;
    }
    let result = result_from_response(&saved, context, true)?;
    touch(context.output);
    Some(result)
}

/// 現像の本体（Tauri に依存しない。テストから直接呼ぶ）。
///
/// `adjustments` は `normalize_adjustments` 済みのもの（コマンドは常に Some）。`slot` は
/// CLI の実行枠（アプリは `DEVELOP_SLOT`。テストは自前の枠を渡して互いに干渉させない）。
pub(crate) fn develop(
    app_data_dir: &Path,
    sidecar: &Path,
    capture_id: &str,
    mode: DevelopMode,
    adjustments: Option<&Value>,
    slot: &DevelopSlot,
    timeout: Duration,
) -> Result<CoreImagePreviewResult, CoreImageError> {
    // 1. 入力の解決（設定 → ARW）。capture_id の形式検査も resolve_arw が FS より先に行う。
    //    どちらも 2 回目以降はメモを使い、config.json の読み直しとフォルダの列挙をしない（#94）。
    let source_root = read_source_root_cached(app_data_dir)?;
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
    let mut request = base_request(&input, mode, adjustments);
    // full のみ JPG を読み取る。継承に使うものと同じバイト列を鍵に含め、
    // ペア追加／更新／削除で必ず再現像する。画像や GPS の値は来歴・ログに出さない。
    let source_jpeg = if mode == DevelopMode::Full {
        fs::canonicalize(source_root.join(format!("{capture_id}.JPG")))
            .ok()
            .filter(|p| p.parent() == input.parent())
            .and_then(|p| fs::read(p).ok())
    } else {
        None
    };
    let mut key_request = request.clone();
    if mode == DevelopMode::Full {
        let fingerprint = source_jpeg.as_ref().map(|b| {
            Sha256::digest(b)
                .iter()
                .map(|v| format!("{v:02x}"))
                .collect::<String>()
        });
        key_request.insert(
            "rust_exif".into(),
            json!({
                "schema": 1, "source_sha256": fingerprint, "software": EXIF_SOFTWARE,
            }),
        );
    }
    let key = cache_key(&key_request, &version, metadata.len(), mtime_ns);

    let (coreimage_dir, output_dir) = prepare_output_dirs(app_data_dir, mode)?;
    // 試験経路の旧出力（coreimage/ 直下）を 1 回だけ掃除する。
    cleanup_legacy_outputs_once(&coreimage_dir);
    // タイムアウトで kill された CLI などが残した古い一時ファイルを掃除する。
    cleanup_stale_temps(&output_dir, STALE_TEMP_AGE);
    // ファイル名は検証済みの capture_id（DSC + 数字 5 桁）と 16 桁の 16 進だけで作る。
    let stem = format!("{capture_id}_{key}");
    let output = output_dir.join(format!("{stem}.jpg"));
    let provenance = output_dir.join(format!("{stem}.json"));
    if output.parent() != Some(output_dir.as_path())
        || provenance.parent() != Some(output_dir.as_path())
    {
        return Err(output_dir_error(
            "出力先が coreimage ディレクトリの外になります",
        ));
    }
    let context = ResultContext {
        capture_id,
        output: &output,
        provenance: &provenance,
        key: &key,
        version: &version,
        adjustments,
    };

    // 3. キャッシュヒットは枠を取らずに返す。
    if let Some(cached) = load_cached(&context) {
        return Ok(cached);
    }

    // 4. CLI は同時 1 本。空くまで待つ（待ちが上限を超えたら BUSY）。枠はガードの drop で返る。
    let _slot = slot.acquire()?;
    // 待っている間に同じ現像（連打など）が終わっていればそれを返す。
    if let Some(cached) = load_cached(&context) {
        return Ok(cached);
    }

    // 5. CLI 呼び出し（ウォッチドッグは子プロセスの起動から数える）。
    request.insert(
        "output".into(),
        Value::String(output.to_string_lossy().into_owned()),
    );
    let body = Value::Object(request).to_string();
    let result = run_sidecar(sidecar, &[], Some(body.as_bytes()), timeout)?;
    let mut response = parse_response(&result)?;
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
    let mut developed = result_from_response(&response, &context, false).ok_or_else(|| {
        CoreImageError::new(
            "SIDECAR_BAD_RESPONSE",
            "Core Image CLI の応答に width / height がありません",
        )
    })?;

    // CLI は EXIF を書かない。full のみ Rust で APP1 を追加する。
    // Python と同じく、読取・構文・サイズ・EXIF 保存の失敗は元の CLI JPEG で続行。
    developed.exif_inherited = source_jpeg.as_deref().is_some_and(|source| {
        inherit_output_exif(&output, source, developed.width, developed.height)
    });
    developed.preview = mode == DevelopMode::Preview;
    response["exif_inherited"] = json!(developed.exif_inherited);
    response["mode"] = json!(mode.as_str());
    // Swift の実応答には request がない。Rust が実際に送った要求を来歴に記録する。
    // #111 は request.mode / input / output を検証してから full を複製する。
    response["request"] = serde_json::from_str(&body).expect("Rust-serialized request JSON");
    // 6. 来歴: preview と同じ CLI 応答 JSON に Rust の継承結果を加える。
    write_provenance(&provenance, &format!("{}\n", response))?;

    // 7. キャッシュの上限管理（いま書いた組は残す）。
    prune_cache(&output_dir, CACHE_MAX_ENTRIES, CACHE_MAX_BYTES, Some(&stem));
    Ok(developed)
}

const EXIF_SOFTWARE: &str = "GeoFrame RAW Assist (Core Image)";

fn inherit_output_exif(output: &Path, source: &[u8], width: u64, height: u64) -> bool {
    let attempt = || -> Option<()> {
        let width = u32::try_from(width).ok()?;
        let height = u32::try_from(height).ok()?;
        let developed = fs::read(output).ok()?;
        let bytes =
            crate::exif_inherit::inherit_exif(&developed, source, width, height, EXIF_SOFTWARE)
                .ok()?;
        let name = output.file_name()?.to_str()?;
        let temp = output.with_file_name(provenance_temp_name(name));
        let saved = create_new_file(&temp)
            .and_then(|mut file| file.write_all(&bytes))
            .and_then(|_| fs::rename(&temp, output));
        if saved.is_err() {
            let _ = fs::remove_file(&temp);
        }
        saved.ok()
    };
    attempt().is_some()
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
// engine_info（Issue #109）: Python の /api/health の代わりにフロントが「エンジン準備完了」を知る
// ---------------------------------------------------------------------------

/// `engine_info` の応答。フロントの `EngineInfo`（coreimage-api.ts）とフィールド名を一致させること。
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct EngineInfo {
    pub cli_version: String,
    pub macos_version: String,
    /// sidecar のパス解決に成功したか。失敗時は reject するので、成功応答では常に true。
    pub sidecar_resolved: bool,
}

/// `sw_vers -productVersion` の出力から版数を取り出す（空なら None）。
fn parse_sw_vers(stdout: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

/// macOS の版数。取れなければ None（呼び出し側で CLI の自己申告へフォールバックする）。
fn macos_product_version() -> Option<String> {
    let output = Command::new("/usr/bin/sw_vers")
        .arg("-productVersion")
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    parse_sw_vers(&output.stdout)
}

fn engine_info_blocking() -> Result<EngineInfo, CoreImageError> {
    let sidecar = sidecar_path()?;
    let version = cli_version(&sidecar)?;
    Ok(EngineInfo {
        cli_version: version.cli_version,
        macos_version: macos_product_version().unwrap_or(version.macos_version),
        sidecar_resolved: true,
    })
}

/// Core Image エンジンの稼働確認。JS からは `invoke("engine_info")`。
/// sidecar が見つからない / `--version` が失敗したら `CoreImageError` で reject する。
#[tauri::command]
pub async fn engine_info() -> Result<EngineInfo, CoreImageError> {
    tauri::async_runtime::spawn_blocking(engine_info_blocking)
        .await
        .map_err(|error| {
            CoreImageError::new("INTERNAL", format!("engine_info が中断されました: {error}"))
        })?
}

// ---------------------------------------------------------------------------
// root_key_for: Python `local_raw/config.py` の `root_key_for` と同じ規則
// ---------------------------------------------------------------------------

/// `root_key` の桁数（Python の `ROOT_KEY_LENGTH`）。
const ROOT_KEY_LENGTH: usize = 8;

/// Python の `Path.resolve()`（非 strict）相当の正規化。相対パスは cwd 基準で絶対化し、
/// `.` / `..` を畳み、存在する最長の祖先は symlink を解決する（`/var` → `/private/var`）。
/// 存在しない残りの成分はそのまま付ける。
fn resolve_path(path: &Path) -> PathBuf {
    use std::path::Component;
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("/"))
            .join(path)
    };
    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    let mut existing = normalized.clone();
    let mut rest: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(real) = fs::canonicalize(&existing) {
            let mut resolved = real;
            for part in rest.iter().rev() {
                resolved.push(part);
            }
            return resolved;
        }
        match (
            existing.file_name().map(|n| n.to_os_string()),
            existing.parent(),
        ) {
            (Some(name), Some(parent)) => {
                rest.push(name);
                existing = parent.to_path_buf();
            }
            _ => return normalized,
        }
    }
}

/// サムネイル名・キャッシュキー用のフォルダ識別子（sha1 の 16 進表記の先頭 8 文字）。
/// 後続の解析移植（Python の `root_key_for` の置き換え）が使う。
pub fn root_key_for(path: &Path) -> String {
    let resolved = resolve_path(path);
    let digest = Sha1::digest(resolved.to_string_lossy().as_bytes());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    hex[..ROOT_KEY_LENGTH].to_string()
}

// ---------------------------------------------------------------------------
// Tauri コマンド
// ---------------------------------------------------------------------------

/// 選択中カットを Core Image でプレビュー現像する。JS からは
/// `invoke("develop_preview_coreimage", { captureId, adjustments })`。
/// `adjustments` はフロントがプリセット既定値 + overrides をマージした 9 キー全量。
///
/// async コマンド + spawn_blocking にしている。同期コマンドは main thread で走るため、
/// 数秒かかる現像（と実行枠の待ち）の間 UI が固まる。
#[tauri::command]
pub async fn develop_preview_coreimage(
    app: AppHandle,
    capture_id: String,
    adjustments: Value,
) -> Result<CoreImagePreviewResult, CoreImageError> {
    develop_command(app, capture_id, adjustments, DevelopMode::Preview).await
}

/// 原寸 JPEG。DevelopedResult 契約（フロント src/lib/types.ts）を直接返す。
#[tauri::command]
pub async fn develop_full_coreimage(
    app: AppHandle,
    capture_id: String,
    adjustments: Value,
    preset: String,
) -> Result<Value, CoreImageError> {
    let preset = crate::presets::get_presets()
        .into_iter()
        .find(|p| p.name == preset)
        .ok_or_else(|| {
            CoreImageError::new(crate::presets::UNKNOWN_PRESET, "未対応のプリセットです")
        })?;
    let result = develop_command(app, capture_id, adjustments, DevelopMode::Full).await?;
    Ok(full_developed_result(&result, &preset))
}

fn full_developed_result(
    result: &CoreImagePreviewResult,
    preset: &crate::presets::PresetInfo,
) -> Value {
    let basename = |path: &str| {
        Path::new(path)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned()
    };
    let lens = result.source.get("lens_model");
    let applied = result
        .applied
        .get("lens_correction_enabled")
        .and_then(Value::as_bool)
        == Some(true);
    let mut correction = json!({ "applied": applied, "lens": lens, "flags": [] });
    if !applied {
        correction["reason"] = json!("coreimage_unsupported");
    }
    // Tauri convertFileSrc と同じ asset URL（macOS）。絶対パス全体を encodeURIComponent。
    let encoded: String = result
        .output_path
        .bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-_.!~*'()".contains(&b) {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect();
    let total = result
        .elapsed_ms
        .get("total")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    let mut value = json!({
        "capture_id": result.capture_id, "preset": preset.name, "preset_label": preset.label,
        "filename": basename(&result.output_path), "recipe_filename": basename(&result.provenance_path),
        "params_hash": result.params_hash.chars().take(10).collect::<String>(),
        "adjustments": result.adjustments, "width": result.width, "height": result.height,
        "elapsed_seconds": (total / 10.0).round() / 100.0,
        "cached": result.cache_hit, "preview": false, "lens_correction": correction,
        "exif_inherited": result.exif_inherited, "output_url": format!("asset://localhost/{encoded}"),
        "engine": "coreimage",
    });
    if let Some(effect) = result.adjustments_applied.get("highlight_recovery") {
        value["highlight_recovery_effect"] = effect.clone();
    }
    value
}

async fn develop_command(
    app: AppHandle,
    capture_id: String,
    adjustments: Value,
    mode: DevelopMode,
) -> Result<CoreImagePreviewResult, CoreImageError> {
    // 安い検査は先に（CLI もファイルシステムも触らない）。
    let adjustments = normalize_adjustments(&adjustments)?;
    if !is_valid_capture_id(&capture_id) {
        return Err(CoreImageError::new(
            "INVALID_CAPTURE_ID",
            format!("撮影IDの形式が不正です: {capture_id:?}"),
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
        develop(
            &app_data_dir,
            &sidecar,
            &capture_id,
            mode,
            Some(&adjustments),
            &DEVELOP_SLOT,
            DEVELOP_TIMEOUT,
        )
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
pub(crate) mod tests {

    #[test]
    fn root_key_matches_python_for_nonexistent_paths() {
        // 期待値は Python で計算: hashlib.sha1(str(Path(p).resolve()).encode()).hexdigest()[:8]
        assert_eq!(root_key_for(Path::new("/")), "42099b4a");
        assert_eq!(
            root_key_for(Path::new("/Users/yoshio/Pictures/日本語 フォルダ")),
            "a7451179"
        );
        // `.` / `..` は畳まれる（/Users/yoshio/b/c）。
        assert_eq!(
            root_key_for(Path::new("/Users/yoshio/a/../b/./c")),
            "2dc61a0d"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn root_key_resolves_system_symlinks_like_python() {
        // Python: Path("/var").resolve() == /private/var -> 91683992
        assert_eq!(root_key_for(Path::new("/var")), "91683992");
        // 存在しない末尾は /tmp -> /private/tmp を解決した上で付く。
        assert_eq!(
            root_key_for(Path::new("/tmp/rk109/none/x/../y")),
            "2e1f43fe"
        );
    }

    #[test]
    fn root_key_follows_symlinks() {
        let dir = TempDir::new("root-key-link");
        let real = dir.path().join("real");
        fs::create_dir(&real).unwrap();
        let link = dir.path().join("link");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert_eq!(root_key_for(&link), root_key_for(&real));
        assert_eq!(root_key_for(&real).len(), 8);
    }

    #[test]
    fn parse_sw_vers_trims_and_rejects_empty() {
        assert_eq!(parse_sw_vers(b"15.1\n"), Some("15.1".to_string()));
        assert_eq!(parse_sw_vers(b"  \n"), None);
    }
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
        touch_file(&dir.path().join("DSC00002.arw"));
        touch_file(&dir.path().join("DSC00002.JPG"));
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
        touch_file(&dir.path().join("DSC00003.ARW"));
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
        touch_file(&dir.path().join("DSC00004.JPG"));
        // 小文字ステムはパターン外（ステムは大文字固定）。
        touch_file(&dir.path().join("dsc00004.ARW"));
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
        touch_file(&target);
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
            "SOURCE_ROOT_NOT_CONFIGURED"
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

    // --- #94: キャッシュヒット時に列挙・config 読み直しをしない ---------------------------
    //
    // 「列挙しなかった」はスレッドローカルの呼び出し回数（probe）で直接確かめる。
    // 「同じ ID の別名の候補を足しても前の結果が返る」という振る舞いでの確かめ方は、テストの
    // 一時ディレクトリが大小文字を区別しない APFS だと `DSC.arw` と `DSC.ARW` を共存させられず
    // 組めないため採らない。

    fn read_dir_calls() -> usize {
        probe::READ_DIR_CALLS.with(|calls| calls.get())
    }

    fn config_reads() -> usize {
        probe::CONFIG_READS.with(|calls| calls.get())
    }

    #[test]
    fn second_resolve_uses_memo_without_enumerating() {
        let dir = TempDir::new("memo-hit");
        touch_file(&dir.path().join("DSC00010.arw"));
        let before = read_dir_calls();
        let first = resolve_arw(dir.path(), "DSC00010").expect("first");
        assert_eq!(read_dir_calls(), before + 1, "1 回目は列挙する");
        let second = resolve_arw(dir.path(), "DSC00010").expect("second");
        assert_eq!(read_dir_calls(), before + 1, "2 回目は列挙しない");
        // 実エントリ名（小文字拡張子）のまま、前回と同じパス。
        assert_eq!(first, second);
        assert_eq!(
            second.file_name().and_then(|n| n.to_str()),
            Some("DSC00010.arw")
        );
        // 別の ID は別のメモなので列挙する。
        touch_file(&dir.path().join("DSC00011.ARW"));
        resolve_arw(dir.path(), "DSC00011").expect("other id");
        assert_eq!(read_dir_calls(), before + 2);
        // 形式検査・root 検査はメモより前（メモがあっても契約どおりのエラー）。
        assert_eq!(
            resolve_arw(dir.path(), "../DSC00010").unwrap_err().code,
            "INVALID_CAPTURE_ID"
        );
    }

    #[test]
    fn memo_is_not_used_when_file_disappears() {
        let dir = TempDir::new("memo-gone");
        let path = dir.path().join("DSC00012.ARW");
        touch_file(&path);
        resolve_arw(dir.path(), "DSC00012").expect("first");
        fs::remove_file(&path).unwrap();
        let before = read_dir_calls();
        assert_eq!(
            resolve_arw(dir.path(), "DSC00012").unwrap_err().code,
            "ARW_NOT_FOUND"
        );
        assert_eq!(read_dir_calls(), before + 1, "メモが使えず列挙し直す");
        // エラーは覚えない: 置き直せば見つかる。
        touch_file(&path);
        assert!(resolve_arw(dir.path(), "DSC00012").is_ok());
    }

    #[test]
    fn memo_is_not_used_when_replaced_by_outside_symlink() {
        let outside = TempDir::new("memo-outside");
        let target = outside.path().join("secret.ARW");
        touch_file(&target);
        let root = TempDir::new("memo-symroot");
        let path = root.path().join("DSC00013.ARW");
        touch_file(&path);
        resolve_arw(root.path(), "DSC00013").expect("first");
        fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink(&target, &path).expect("symlink");
        let before = read_dir_calls();
        assert_eq!(
            resolve_arw(root.path(), "DSC00013").unwrap_err().code,
            "ARW_OUTSIDE_SOURCE_ROOT"
        );
        assert_eq!(read_dir_calls(), before + 1);
        // 失敗は覚えないので、もう一度呼んでも同じエラー（メモから通さない）。
        assert_eq!(
            resolve_arw(root.path(), "DSC00013").unwrap_err().code,
            "ARW_OUTSIDE_SOURCE_ROOT"
        );
    }

    #[test]
    fn memo_is_re_resolved_when_replaced_by_inside_symlink() {
        let root = TempDir::new("memo-inside");
        let path = root.path().join("DSC00014.ARW");
        touch_file(&path);
        let other = root.path().join("other.bin");
        touch_file(&other);
        resolve_arw(root.path(), "DSC00014").expect("first");
        fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink(&other, &path).expect("symlink");
        let before = read_dir_calls();
        // 従来の列挙経路と同じ結果（フォルダ内を指すリンクは解決先を返す）。
        let resolved = resolve_arw(root.path(), "DSC00014").expect("re-resolve");
        assert_eq!(read_dir_calls(), before + 1, "メモは使わず解決し直す");
        assert_eq!(resolved, other.canonicalize().unwrap());
    }

    #[test]
    fn source_root_cache_rereads_when_config_changes() {
        let app = TempDir::new("config-cache");
        let config = app.path().join("config.json");
        // 同じ長さの値にして、サイズではなく mtime の変化で読み直すことを確かめる。
        fs::write(&config, r#"{"source_root":"/Volumes/A"}"#).unwrap();
        set_age(&config, Duration::from_secs(120));
        let before = config_reads();
        assert_eq!(
            read_source_root_cached(app.path()).unwrap(),
            PathBuf::from("/Volumes/A")
        );
        assert_eq!(
            read_source_root_cached(app.path()).unwrap(),
            PathBuf::from("/Volumes/A")
        );
        assert_eq!(config_reads(), before + 1, "2 回目は読み直さない");

        // 同じ inode・同じサイズのまま中身と mtime だけ変える（rename ではなく上書き）。
        fs::File::options()
            .write(true)
            .truncate(true)
            .open(&config)
            .unwrap()
            .write_all(br#"{"source_root":"/Volumes/B"}"#)
            .unwrap();
        set_age(&config, Duration::from_secs(60));
        assert_eq!(
            read_source_root_cached(app.path()).unwrap(),
            PathBuf::from("/Volumes/B")
        );
        assert_eq!(config_reads(), before + 2);

        // エラーは覚えず、read_source_root と同じコードを返す。
        fs::write(&config, "{not json").unwrap();
        assert_eq!(
            read_source_root_cached(app.path()).unwrap_err().code,
            "SOURCE_ROOT_NOT_CONFIGURED"
        );
        fs::remove_file(&config).unwrap();
        assert_eq!(
            read_source_root_cached(app.path()).unwrap_err().code,
            "SOURCE_ROOT_NOT_CONFIGURED"
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

    /// stdin を読まずに `body` を実行して終わる偽サイドカー（sh スクリプト）。
    fn script_sidecar(dir: &Path, body: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.join("no-stdin-cli");
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    /// パイプの容量（macOS は 16〜64KB）を大きく超えるリクエスト。子が読まずに終われば
    /// write_all は必ず EPIPE で失敗する。
    fn oversized_request() -> Vec<u8> {
        vec![b' '; 4 * 1024 * 1024]
    }

    #[test]
    fn sidecar_stdin_write_failure_keeps_structured_cli_error() {
        let dir = TempDir::new("stdin-epipe-json");
        let program = script_sidecar(
            dir.path(),
            r#"printf '%s\n' '{"ok":false,"error":{"code":"INPUT_NOT_FOUND","message":"input does not exist"}}'
exit 1"#,
        );
        let started = Instant::now();
        let output = run_sidecar(
            &program,
            &[],
            Some(&oversized_request()),
            Duration::from_secs(10),
        )
        .expect("stdout の構造化エラーを返す（SIDECAR_IO_FAILED にしない）");
        assert!(!output.status.success());
        let response = parse_response(&output).unwrap();
        let error = cli_error(&response, output.status);
        assert_eq!(error.code, "INPUT_NOT_FOUND");
        assert_eq!(error.message, "input does not exist");
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn sidecar_stdin_write_failure_without_structured_error_is_io_failed() {
        let dir = TempDir::new("stdin-epipe-none");
        // stdout が空 / JSON でない / 成功応答（リクエストを渡し切れていないので信用しない）。
        let bodies = [
            "exit 1",
            "echo 'not json'\nexit 1",
            r#"printf '%s\n' '{"ok":true}'"#,
            r#"printf '%s\n' '{"ok":false,"error":{"code":"X"}}'
exit 1"#,
        ];
        for body in bodies {
            let program = script_sidecar(dir.path(), body);
            let error = run_sidecar(
                &program,
                &[],
                Some(&oversized_request()),
                Duration::from_secs(10),
            )
            .unwrap_err();
            assert_eq!(error.code, "SIDECAR_IO_FAILED", "{body}");
        }
    }

    #[test]
    fn sidecar_stdin_write_failure_still_honours_watchdog() {
        let dir = TempDir::new("stdin-epipe-hang");
        // stdin を閉じてから居座る（書き込みは EPIPE、終了しないのでウォッチドッグで止める）。
        let program = script_sidecar(dir.path(), "exec 0<&-\nexec sleep 30");
        let started = Instant::now();
        let error = run_sidecar(
            &program,
            &[],
            Some(&oversized_request()),
            Duration::from_millis(500),
        )
        .unwrap_err();
        assert_eq!(error.code, "SIDECAR_TIMEOUT");
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    // -----------------------------------------------------------------------
    // バッチセッション（Issue #82）
    // -----------------------------------------------------------------------

    use super::batch::BatchSession;

    /// `--batch` の偽 CLI。1 行読むごとに番号を数え、行の中身で振る舞いを変える。
    /// 応答の後に stderr へ 1 行ログを書く（本物の CLI と同じく stderr も流れる）。
    fn batch_mock(dir: &Path) -> PathBuf {
        script_sidecar(
            dir,
            r#"[ "$1" = "--batch" ] || exit 9
n=0
while IFS= read -r line; do
  n=$((n+1))
  case "$line" in
    *fail*) printf '{"error":{"code":"INPUT_NOT_FOUND","message":"missing"},"ok":false,"sequence":%d}\n' "$n" ;;
    *hang*) exec sleep 30 ;;
    *skew*) printf '{"ok":true,"sequence":%d}\n' "$((n+5))" ;;
    *noseq*) printf '{"ok":true}\n' ;;
    *garbage*) echo 'not json' ;;
    *die*) echo "dying at $n" >&2; exit 3 ;;
    *) printf '{"ok":true,"sequence":%d}\n' "$n" ;;
  esac
  echo "handled $n" >&2
done
exit 0"#,
        )
    }

    fn process_alive(pid: u32) -> bool {
        Command::new("kill")
            .args(["-0", &pid.to_string()])
            .stderr(Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
    }

    #[test]
    fn batch_session_starts_runs_requests_and_finishes() {
        let dir = TempDir::new("batch-ok");
        let mut session =
            BatchSession::start(&batch_mock(dir.path()), Duration::from_secs(10)).unwrap();
        assert!(session.is_usable());
        for expected in 1..=3u64 {
            let response = session.request(&json!({"cmd": "ok"})).unwrap();
            assert_eq!(response["sequence"], json!(expected));
        }
        assert_eq!(session.sent(), 3);
        let started = Instant::now();
        let status = session.finish().unwrap();
        assert!(status.success());
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn batch_session_cli_error_keeps_session_usable() {
        let dir = TempDir::new("batch-cli-error");
        let mut session =
            BatchSession::start(&batch_mock(dir.path()), Duration::from_secs(10)).unwrap();
        session.request(&json!({"cmd": "ok"})).unwrap();
        let error = session.request(&json!({"cmd": "fail"})).unwrap_err();
        assert_eq!(error.code, "INPUT_NOT_FOUND");
        assert_eq!(error.message, "missing");
        assert!(session.is_usable());
        let response = session.request(&json!({"cmd": "ok"})).unwrap();
        assert_eq!(response["sequence"], json!(3));
        assert!(session.finish().unwrap().success());
    }

    #[test]
    fn batch_session_timeout_kills_child_and_closes_session() {
        let dir = TempDir::new("batch-timeout");
        let mut session =
            BatchSession::start(&batch_mock(dir.path()), Duration::from_millis(300)).unwrap();
        session.request(&json!({"cmd": "ok"})).unwrap();
        let pid = session.pid().unwrap();
        let started = Instant::now();
        let error = session.request(&json!({"cmd": "hang"})).unwrap_err();
        assert_eq!(error.code, "SIDECAR_TIMEOUT");
        assert!(started.elapsed() < Duration::from_secs(5));
        assert!(!session.is_usable());
        assert!(
            !process_alive(pid),
            "タイムアウトした子プロセスは kill・回収済み"
        );
        let error = session.request(&json!({"cmd": "ok"})).unwrap_err();
        assert_eq!(error.code, "SIDECAR_SESSION_CLOSED");
        assert_eq!(session.finish().unwrap_err().code, "SIDECAR_SESSION_CLOSED");
    }

    #[test]
    fn batch_session_rejects_mismatched_or_unreadable_responses() {
        let dir = TempDir::new("batch-mismatch");
        let program = batch_mock(dir.path());
        for cmd in ["skew", "noseq", "garbage", "die"] {
            let mut session = BatchSession::start(&program, Duration::from_secs(10)).unwrap();
            session.request(&json!({"cmd": "ok"})).unwrap();
            let pid = session.pid().unwrap();
            let error = session.request(&json!({ "cmd": cmd })).unwrap_err();
            assert_eq!(error.code, "SIDECAR_BAD_RESPONSE", "{cmd}");
            assert!(!session.is_usable(), "{cmd}");
            assert!(!process_alive(pid), "{cmd}");
            if cmd == "die" {
                assert!(error.message.contains("dying at 2"), "{}", error.message);
            }
        }
    }

    #[test]
    fn batch_session_drop_reaps_child_that_ignores_eof() {
        let dir = TempDir::new("batch-drop");
        // stdin を読まずに居座る（EOF では終わらない）。Drop の猶予の後に kill される。
        let program = script_sidecar(dir.path(), "exec sleep 30");
        let session = BatchSession::start(&program, Duration::from_secs(10)).unwrap();
        let pid = session.pid().unwrap();
        assert!(process_alive(pid));
        let started = Instant::now();
        drop(session);
        assert!(started.elapsed() < Duration::from_secs(5));
        assert!(!process_alive(pid));
    }

    #[test]
    fn batch_session_drop_closes_stdin_for_graceful_exit() {
        let dir = TempDir::new("batch-drop-eof");
        let mut session =
            BatchSession::start(&batch_mock(dir.path()), Duration::from_secs(10)).unwrap();
        session.request(&json!({"cmd": "ok"})).unwrap();
        let pid = session.pid().unwrap();
        drop(session);
        assert!(!process_alive(pid));
    }

    #[test]
    fn batch_session_finish_times_out_when_child_ignores_eof() {
        let dir = TempDir::new("batch-finish-timeout");
        let program = script_sidecar(dir.path(), "exec sleep 30");
        let session = BatchSession::start(&program, Duration::from_millis(300)).unwrap();
        let pid = session.pid().unwrap();
        let started = Instant::now();
        let error = session.finish().unwrap_err();
        assert_eq!(error.code, "SIDECAR_TIMEOUT");
        assert!(started.elapsed() < Duration::from_secs(5));
        assert!(!process_alive(pid));
    }

    #[test]
    fn batch_session_spawn_failure() {
        let error = BatchSession::start(
            Path::new("/nonexistent/geoframe-ci-develop"),
            Duration::from_secs(1),
        )
        .err()
        .unwrap();
        assert_eq!(error.code, "SIDECAR_SPAWN_FAILED");
    }

    /// 本物の CLI（externalBin）と `--batch` の契約が合っているか。ARW は使わない（無ければ skip）。
    #[test]
    fn batch_session_with_built_sidecar() {
        let Some(sidecar) = built_sidecar() else {
            eprintln!("skip: src-tauri/binaries/geoframe-ci-develop-* がありません");
            return;
        };
        let mut session = BatchSession::start(&sidecar, Duration::from_secs(30)).unwrap();
        let error = session.request(&json!({})).unwrap_err();
        assert_eq!(error.code, "INVALID_REQUEST");
        let error = session
            .request(&json!({
                "input": "/nonexistent/geoframe-batch-test/DSC00001.ARW",
                "output": "/nonexistent/geoframe-batch-test/out.jpg",
                "mode": "preview"
            }))
            .unwrap_err();
        assert_eq!(error.code, "INPUT_NOT_FOUND");
        assert!(session.is_usable());
        assert_eq!(session.sent(), 2);
        assert!(session.finish().unwrap().success());
    }

    #[test]
    fn sidecar_sha256_hashes_binary_and_memoizes_per_path() {
        let dir = TempDir::new("sidecar-sha");
        let a = dir.path().join("a");
        let b = dir.path().join("b");
        fs::write(&a, b"abc").unwrap();
        fs::write(&b, b"abd").unwrap();
        // SHA-256("abc") の既知値。
        assert_eq!(
            sidecar_sha256(&a).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_ne!(sidecar_sha256(&a).unwrap(), sidecar_sha256(&b).unwrap());
        // パスごとに 1 回だけ計算する（起動中に中身が変わっても読み直さない）。
        fs::write(&a, b"changed").unwrap();
        assert_eq!(
            sidecar_sha256(&a).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let error = sidecar_sha256(&dir.path().join("missing")).unwrap_err();
        assert_eq!(error.code, "SIDECAR_IO_FAILED");
    }

    #[test]
    fn provenance_temp_is_created_new_and_never_follows_existing_entries() {
        let dir = TempDir::new("provenance");
        let target = dir.path().join("DSC00001.json");
        write_provenance(&target, "{\"ok\":true}").unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "{\"ok\":true}");
        // 2 回目は上書き（rename で置き換える）
        write_provenance(&target, "{\"ok\":2}").unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "{\"ok\":2}");
        // 既にある名前（シンボリックリンク）は create_new で拒まれ、リンク先は書き換わらない
        let victim = dir.path().join("victim.txt");
        fs::write(&victim, "keep").unwrap();
        let link = dir.path().join("link.json");
        std::os::unix::fs::symlink(&victim, &link).unwrap();
        let error = create_new_file(&link).unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::AlreadyExists);
        assert_eq!(fs::read_to_string(&victim).unwrap(), "keep");
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

    fn sample_adjustments() -> Value {
        normalize_adjustments(&json!({
            "exposure": 0.0,
            "wb_temp": 0.0,
            "shadow_lift": 15.0,
            "highlight_recovery": 60.0,
            "brightness": 1.0,
            "contrast": 1.03,
            "saturation": 1.02,
            "sharpness": 1.04,
            "noise_reduction": "light"
        }))
        .unwrap()
    }

    fn test_version(cli: &str) -> CliVersion {
        CliVersion {
            fingerprint: format!("{{\"cli_version\":\"{cli}\",\"macos_version\":\"27.0.1\"}}"),
            cli_version: cli.into(),
            macos_version: "27.0.1".into(),
            binary_sha256: "0".repeat(64),
        }
    }

    #[test]
    fn base_request_respects_cli_contract() {
        let input = Path::new("/abs/DSC00001.ARW");
        let adjustments = sample_adjustments();
        let preview = base_request(input, DevelopMode::Preview, Some(&adjustments));
        assert_eq!(preview.get("input"), Some(&json!("/abs/DSC00001.ARW")));
        assert_eq!(preview.get("mode"), Some(&json!("preview")));
        assert_eq!(preview.get("max_edge"), Some(&json!(2400)));
        assert_eq!(preview.get("format"), Some(&json!("jpeg")));
        assert_eq!(preview.get("decoder_version"), Some(&json!("9")));
        assert_eq!(
            preview.get("settings"),
            Some(&json!({"profile": "apple_default", "lens_correction": true}))
        );
        assert_eq!(preview.get("adjustments"), Some(&adjustments));
        assert!(preview.get("output").is_none(), "output は鍵の後で足す");
        // settings に exposure_ev / neutral_temperature / neutral_tint を入れない
        // （adjustments との同時指定は CLI 0.3.0 で INVALID_REQUEST。§4.1）。
        let settings = preview["settings"].as_object().unwrap();
        for key in ["exposure_ev", "neutral_temperature", "neutral_tint"] {
            assert!(!settings.contains_key(key));
        }
        let mut keys: Vec<&str> = preview.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            [
                "adjustments",
                "decoder_version",
                "format",
                "input",
                "max_edge",
                "mode",
                "settings"
            ]
        );

        let plain = base_request(input, DevelopMode::Preview, None);
        assert!(
            plain.get("adjustments").is_none(),
            "None なら既定リクエスト"
        );
        let full = base_request(input, DevelopMode::Full, Some(&adjustments));
        assert!(
            full.get("max_edge").is_none(),
            "full に max_edge を付けると CLI がエラーにする"
        );
        assert!(full.get("jpeg_quality").is_none());
    }

    #[test]
    fn canonical_json_ignores_key_order() {
        let a = json!({"b": 1, "a": {"y": [1, {"q": 1, "p": 2}], "x": "s"}});
        let mut reordered = serde_json::Map::new();
        reordered.insert("a".into(), json!({"x": "s", "y": [1, {"p": 2, "q": 1}]}));
        reordered.insert("b".into(), json!(1));
        assert_eq!(
            canonical_json(&a),
            canonical_json(&Value::Object(reordered))
        );
        assert_eq!(
            canonical_json(&a),
            r#"{"a":{"x":"s","y":[1,{"p":2,"q":1}]},"b":1}"#
        );
        assert_eq!(canonical_json(&json!("a\"b")), r#""a\"b""#);
    }

    #[test]
    fn cache_key_is_stable_and_sensitive() {
        let version = test_version("0.3.0");
        let adjustments = sample_adjustments();
        let input = Path::new("/abs/DSC00001.ARW");
        let request = base_request(input, DevelopMode::Preview, Some(&adjustments));
        let key = cache_key(&request, &version, 100, 5);
        assert_eq!(key.len(), 16);
        assert!(key
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        assert_eq!(key, cache_key(&request, &version, 100, 5));

        // output は鍵に入らない。
        let mut with_output = request.clone();
        with_output.insert("output".into(), json!("/somewhere/x.jpg"));
        assert_eq!(key, cache_key(&with_output, &version, 100, 5));

        // ARW の同一性。
        assert_ne!(key, cache_key(&request, &version, 101, 5));
        assert_ne!(key, cache_key(&request, &version, 100, 6));
        // CLI の --version 全文（cli_version も macos_version も）。
        assert_ne!(key, cache_key(&request, &test_version("0.3.1"), 100, 5));
        let other_os = CliVersion {
            fingerprint: version.fingerprint.replace("27.0.1", "27.1"),
            ..version.clone()
        };
        assert_ne!(key, cache_key(&request, &other_os, 100, 5));
        // CLI 実行ファイルの SHA-256（版も OS も同じまま実装だけ変わった場合）。
        let rebuilt = CliVersion {
            binary_sha256: "1".repeat(64),
            ..version.clone()
        };
        assert_ne!(key, cache_key(&request, &rebuilt, 100, 5));
        // mode / 入力パス / settings / adjustments の有無。
        let full = base_request(input, DevelopMode::Full, Some(&adjustments));
        assert_ne!(key, cache_key(&full, &version, 100, 5));
        let other_path = base_request(
            Path::new("/other/DSC00001.ARW"),
            DevelopMode::Preview,
            Some(&adjustments),
        );
        assert_ne!(key, cache_key(&other_path, &version, 100, 5));
        let plain = base_request(input, DevelopMode::Preview, None);
        assert_ne!(key, cache_key(&plain, &version, 100, 5));
        let mut lens_off = request.clone();
        lens_off.insert(
            "settings".into(),
            json!({"profile": "apple_default", "lens_correction": false}),
        );
        assert_ne!(key, cache_key(&lens_off, &version, 100, 5));

        // adjustments の 1 キーだけを変えても鍵が変わる（9 キーすべて）。
        let changed: [(&str, Value); 9] = [
            ("exposure", json!(0.1)),
            ("wb_temp", json!(1.0)),
            ("shadow_lift", json!(16.0)),
            ("highlight_recovery", json!(61.0)),
            ("brightness", json!(1.01)),
            ("contrast", json!(1.04)),
            ("saturation", json!(1.03)),
            ("sharpness", json!(1.05)),
            ("noise_reduction", json!("full")),
        ];
        let mut seen = std::collections::HashSet::from([key.clone()]);
        for (name, value) in changed {
            let mut modified = adjustments.clone();
            modified[name] = value;
            let modified = normalize_adjustments(&modified).unwrap();
            let other = cache_key(
                &base_request(input, DevelopMode::Preview, Some(&modified)),
                &version,
                100,
                5,
            );
            assert!(seen.insert(other), "{name} を変えたら鍵が変わる");
        }

        // 正規化で同値になる入力（int と float、キー順）は同じ鍵。
        let mut as_int = adjustments.clone();
        as_int["shadow_lift"] = json!(15);
        let as_int = normalize_adjustments(&as_int).unwrap();
        assert_eq!(
            key,
            cache_key(
                &base_request(input, DevelopMode::Preview, Some(&as_int)),
                &version,
                100,
                5
            )
        );
    }

    #[test]
    fn develop_slot_waits_then_busy_over_limit() {
        use std::sync::{mpsc, Arc};
        let slot = Arc::new(DevelopSlot::new(2));
        let running = slot.acquire().expect("空いているので取れる");
        assert_eq!(slot.pending(), 1);

        // 待ち 2 件はブロックして並ぶ。
        let (sender, receiver) = mpsc::channel();
        let waiters: Vec<_> = (0..2)
            .map(|index| {
                let slot = Arc::clone(&slot);
                let sender = sender.clone();
                thread::spawn(move || {
                    let guard = slot.acquire().expect("待ちの上限内");
                    sender.send(index).unwrap();
                    drop(guard);
                })
            })
            .collect();
        let deadline = Instant::now() + Duration::from_secs(5);
        while slot.pending() < 3 {
            assert!(Instant::now() < deadline, "待ちが並ばない");
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            receiver.recv_timeout(Duration::from_millis(100)).is_err(),
            "走行中は待ちが先へ進まない"
        );

        // 走行 1 + 待ち 2 を超える 4 件目は待たずに BUSY。
        let started = Instant::now();
        let error = slot.acquire().err().expect("BUSY になる");
        assert_eq!(error.code, "BUSY");
        assert!(
            started.elapsed() < Duration::from_secs(1),
            "BUSY は待たない"
        );
        assert_eq!(slot.pending(), 3, "BUSY は件数を増やさない");

        drop(running);
        for handle in waiters {
            handle.join().unwrap();
        }
        let mut done: Vec<i32> = receiver.try_iter().collect();
        done.sort_unstable();
        assert_eq!(done, [0, 1]);
        assert_eq!(slot.pending(), 0);
        assert!(slot.acquire().is_ok(), "全部返ったら取れる");
    }

    #[test]
    fn develop_slot_is_released_on_error_and_panic() {
        let slot = DevelopSlot::new(0);
        {
            let _guard = slot.acquire().expect("空いている");
            assert_eq!(
                slot.acquire().err().map(|e| e.code).as_deref(),
                Some("BUSY")
            );
        }
        assert!(slot.acquire().is_ok(), "drop で解放される");

        fn fails(slot: &DevelopSlot) -> Result<(), CoreImageError> {
            let _guard = slot.acquire()?;
            Err(CoreImageError::new("X", "x"))
        }
        assert!(fails(&slot).is_err());
        assert_eq!(slot.pending(), 0);

        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = slot.acquire().expect("slot");
            panic!("boom");
        }));
        assert!(result.is_err());
        assert_eq!(slot.pending(), 0);
        assert!(slot.acquire().is_ok(), "panic 後（poison）でも取れる");
    }

    fn set_age(path: &Path, age: Duration) {
        fs::File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_modified(SystemTime::now() - age)
            .unwrap();
    }

    fn write_bytes(path: &Path, len: usize) {
        fs::write(path, vec![b'x'; len]).unwrap();
    }

    #[test]
    fn prune_cache_removes_oldest_pairs_to_80_percent() {
        let dir = TempDir::new("prune");
        // 10 組（古い順に 0..9）。各組 jpg 100B + json 10B。
        let stems: Vec<String> = (0..10).map(|i| format!("DSC0000{i}_{:016x}", i)).collect();
        for (index, stem) in stems.iter().enumerate() {
            let age = Duration::from_secs(1000 - index as u64 * 10);
            for (ext, len) in [("jpg", 100), ("json", 10)] {
                let path = dir.path().join(format!("{stem}.{ext}"));
                write_bytes(&path, len);
                set_age(&path, age);
            }
        }
        // 対象外のもの: 形の違う名前、古い一時ファイル、外を指すシンボリックリンク、ディレクトリ。
        let other = dir.path().join("notes.txt");
        write_bytes(&other, 10_000);
        set_age(&other, Duration::from_secs(5000));
        let legacy = dir.path().join(format!("DSC00001_preview_{:016x}.jpg", 1));
        touch_file(&legacy);
        set_age(&legacy, Duration::from_secs(5000));
        let outside = TempDir::new("prune-outside");
        let target = outside.path().join(format!("DSC09999_{:016x}.jpg", 9));
        write_bytes(&target, 10_000);
        let link = dir.path().join(format!("DSC09998_{:016x}.jpg", 8));
        std::os::unix::fs::symlink(&target, &link).unwrap();
        fs::create_dir(dir.path().join(format!("DSC09997_{:016x}.jpg", 7))).unwrap();

        // 上限内なら何もしない。
        assert_eq!(prune_cache(dir.path(), 10, 1_000_000, None), 0);
        // 件数超過（上限 5 → 目標 4 件）: 古い 6 組を消す。最古の組は keep_stem で守る。
        let removed = prune_cache(dir.path(), 5, 1_000_000, Some(&stems[0]));
        assert_eq!(removed, 6);
        let alive = |stem: &str| dir.path().join(format!("{stem}.jpg")).exists();
        assert!(alive(&stems[0]), "keep_stem は消さない");
        for stem in &stems[1..7] {
            assert!(!alive(stem), "{stem} は古いので消える");
            assert!(
                !dir.path().join(format!("{stem}.json")).exists(),
                "組ごと消える"
            );
        }
        for stem in &stems[7..] {
            assert!(alive(stem), "{stem} は新しいので残る");
        }
        assert!(other.exists() && legacy.exists());
        assert!(fs::symlink_metadata(&link).is_ok() && target.exists());

        // 容量超過（残り 4 組 = 440B、上限 300B → 目標 240B）: 古い 2 組を消す。
        let removed = prune_cache(dir.path(), 100, 300, None);
        assert_eq!(removed, 2);
        assert!(!alive(&stems[0]) && !alive(&stems[7]));
        assert!(alive(&stems[8]) && alive(&stems[9]));
    }

    fn touch_file(path: &Path) {
        fs::write(path, b"x").expect("write file");
    }

    #[test]
    fn cleanup_legacy_outputs_only_touches_old_experiment_names() {
        let dir = TempDir::new("legacy");
        let key = "0123456789abcdef";
        let legacy = [
            format!("DSC00001_preview_{key}.jpg"),
            format!("DSC00001_preview_{key}.json"),
            format!("DSC00002_full_{key}.jpg"),
            format!("DSC00002_full_{key}.json"),
        ];
        let kept = [
            "config.json".to_string(),
            format!("DSC00001_{key}.jpg"),
            format!("DSC00001_preview_{key}.png"),
            format!("DSC00001_preview_{}.jpg", "0123"),
            format!("dsc00009_preview_{key}.jpg"),
        ];
        for name in legacy.iter().chain(kept.iter()) {
            touch_file(&dir.path().join(name));
        }
        // サブディレクトリ（新経路の preview/）の中は再帰しない。
        let sub = dir.path().join("preview");
        fs::create_dir(&sub).unwrap();
        let nested = sub.join(format!("DSC00003_preview_{key}.jpg"));
        touch_file(&nested);
        // 旧名の形をした、外を指すシンボリックリンクはリンクもリンク先も消さない。
        let outside = TempDir::new("legacy-outside");
        let target = outside.path().join("victim.jpg");
        touch_file(&target);
        let link = dir.path().join(format!("DSC00004_preview_{key}.jpg"));
        std::os::unix::fs::symlink(&target, &link).unwrap();

        assert_eq!(cleanup_legacy_outputs(dir.path()), legacy.len());
        for name in &legacy {
            assert!(!dir.path().join(name).exists(), "{name} は消える");
        }
        for name in &kept {
            assert!(dir.path().join(name).exists(), "{name} は残る");
        }
        assert!(nested.exists());
        assert!(fs::symlink_metadata(&link).is_ok() && target.exists());
    }

    #[test]
    fn cleanup_removes_only_stale_dot_tmp_files_in_preview_dir() {
        let app_data = TempDir::new("cleanup");
        let (_, preview_dir) = prepare_output_dirs(app_data.path(), DevelopMode::Preview).unwrap();
        assert!(preview_dir.ends_with("coreimage/preview"));
        let make = |name: &str, stale: bool| {
            let path = preview_dir.join(name);
            touch_file(&path);
            if stale {
                set_age(&path, Duration::from_secs(11 * 60));
            }
            path
        };
        let stale_tmp = make(".x.jpg.ABC.tmp", true);
        let fresh_tmp = make(".y.jpg.DEF.tmp", false);
        let stale_jpg = make("DSC00001_0123456789abcdef.jpg", true);
        let stale_no_dot = make("z.tmp", true);
        // 外を指す古いシンボリックリンクは、リンク先もリンク自体も消さない。
        let outside = TempDir::new("cleanup-outside");
        let target = outside.path().join(".target.tmp");
        touch_file(&target);
        set_age(&target, Duration::from_secs(11 * 60));
        let link = preview_dir.join(".link.tmp");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        // 先頭ドット + .tmp のディレクトリも対象外。
        let tmp_dir = preview_dir.join(".dir.tmp");
        fs::create_dir(&tmp_dir).unwrap();

        cleanup_stale_temps(&preview_dir, STALE_TEMP_AGE);

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

    #[test]
    fn output_dirs_reject_symlink_escape() {
        let app_data = TempDir::new("escape");
        let outside = TempDir::new("escape-outside");
        fs::create_dir(app_data.path().join(OUTPUT_DIR_NAME)).unwrap();
        std::os::unix::fs::symlink(
            outside.path(),
            app_data.path().join(OUTPUT_DIR_NAME).join("preview"),
        )
        .unwrap();
        let error = prepare_output_dirs(app_data.path(), DevelopMode::Preview).unwrap_err();
        assert_eq!(error.code, "OUTPUT_DIR_FAILED");

        let app_data = TempDir::new("escape2");
        std::os::unix::fs::symlink(outside.path(), app_data.path().join(OUTPUT_DIR_NAME)).unwrap();
        let error = prepare_output_dirs(app_data.path(), DevelopMode::Preview).unwrap_err();
        assert_eq!(error.code, "OUTPUT_DIR_FAILED");
        assert!(
            fs::read_dir(outside.path()).unwrap().next().is_none(),
            "外には何も作らない"
        );
    }

    // ---- CLI 0.3.0 のモック（実バイナリは 0.2.0 なので、0.3.0 の応答はスクリプトで返す） ----

    /// 0.3.0 の契約（§4.2）どおりの応答を返す sh スクリプト。リクエスト全文を応答の
    /// `request` に入れて返すので、来歴 JSON から CLI に何を渡したかを検査できる。
    /// 呼ばれるたびに `calls` へ 1 行足す（キャッシュヒットで CLI を呼ばないことの確認用）。
    fn mock_cli(dir: &Path, body: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.join("mock-cli");
        let calls = dir.join("calls");
        let script = format!(
            r#"#!/bin/sh
if [ "$1" = "--version" ]; then
  printf '%s\n' '{{"cli_version":"0.6.0","engine":"coreimage","macos_version":"27.0.1","ok":true}}'
  exit 0
fi
echo run >> '{calls}'
req=$(cat)
out=$(printf '%s' "$req" | sed -n 's/.*"output":"\([^"]*\)".*/\1/p')
{body}
"#,
            calls = calls.display()
        );
        fs::write(&path, script).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    const MOCK_OK: &str = r#"printf 'JPEGDATA' > "$out"
printf '{"ok":true,"cli_version":"0.6.0","macos_version":"27.0.1","decoder_version":"9","mode":"preview","format":"jpeg","output":"%s","width":2400,"height":1600,"elapsed_ms":{"decode":1.0,"render_encode":2.0,"total":3.0},"applied":{"profile":"apple_default","lens_correction_enabled":true,"sharpness_amount":1},"adjustments_applied":{"defaults":{"sharpness_amount":1.0},"post":{"shadow_lift":0.55},"highlight_recovery":{"value":60.0,"exposure_delta":-0.7,"shadow_lift_delta":40.0,"effective_exposure":-0.7,"effective_shadow_lift":55.0,"shadow_lift_clamped":false},"ignored":[],"guards":[]},"source":{"lens_model":"E 16mm F2.8","iso":3200,"camera_model":"ILCE-6400"},"request":%s}\n' "$out" "$req""#;

    const MOCK_FAIL: &str = r#"printf '%s\n' '{"ok":false,"error":{"code":"RENDER_FAILED","message":"boom"}}'
exit 1"#;

    fn calls(dir: &Path) -> usize {
        fs::read_to_string(dir.join("calls"))
            .map(|text| text.lines().count())
            .unwrap_or(0)
    }

    /// 偽の app_data_dir（config.json → ソースフォルダ）を作る。
    fn fake_app(tag: &str, ids: &[&str]) -> (TempDir, TempDir) {
        let app_data = TempDir::new(&format!("{tag}-app"));
        let source = TempDir::new(&format!("{tag}-src"));
        for id in ids {
            fs::write(source.path().join(format!("{id}.ARW")), b"fake raw").unwrap();
        }
        fs::write(
            app_data.path().join("config.json"),
            json!({"config_version": 1, "source_root": source.path()}).to_string(),
        )
        .unwrap();
        (app_data, source)
    }

    /// export の結合テスト用。本物の Swift と同じく request を返さない CLI モック。
    pub(crate) fn full_for_export_test(app: &Path) -> CoreImagePreviewResult {
        let bin = TempDir::new("export-full-cli");
        let mock = MOCK_OK
            .replace(",\"request\":%s", "")
            .replace("\"$out\" \"$req\"", "\"$out\"");
        let cli = mock_cli(bin.path(), &mock);
        develop(
            app,
            &cli,
            "DSC00001",
            DevelopMode::Full,
            Some(&sample_adjustments()),
            &DevelopSlot::new(2),
            Duration::from_secs(5),
        )
        .unwrap()
    }

    #[test]
    fn full_develop_inherits_exif_caches_and_handles_pair_changes() {
        use crate::exif_inherit::tests::{fixture, jpeg};
        let (app, source) = fake_app("full-exif", &["DSC00001"]);
        let bin = TempDir::new("full-exif-cli");
        let cli_image = bin.path().join("developed.jpg");
        let plain = jpeg(&[(0xe2, b"ICC_PROFILE\0synthetic")]);
        fs::write(&cli_image, &plain).unwrap();
        let mock = MOCK_OK
            .replace(
                "printf 'JPEGDATA' > \"$out\"",
                &format!("command cp -f '{}' \"$out\"", cli_image.display()),
            )
            .replace("\"width\":2400", "\"width\":80")
            .replace("\"height\":1600", "\"height\":40")
            .replace(",\"request\":%s", "")
            .replace("\"$out\" \"$req\"", "\"$out\"");
        let cli = mock_cli(bin.path(), &mock);
        let pair = source.path().join("DSC00001.JPG");
        let original = fixture(false, true, Some(8));
        fs::write(&pair, &original).unwrap();
        let slot = DevelopSlot::new(2);
        let adjustments = sample_adjustments();
        let run = || {
            develop(
                app.path(),
                &cli,
                "DSC00001",
                DevelopMode::Full,
                Some(&adjustments),
                &slot,
                Duration::from_secs(5),
            )
            .unwrap()
        };
        let first = run();
        assert!(!first.preview && first.exif_inherited && !first.cache_hit);
        assert_eq!(
            Path::new(&first.output_path).parent().unwrap(),
            app.path().join("coreimage/full").canonicalize().unwrap()
        );
        let saved: Value =
            serde_json::from_slice(&fs::read(&first.provenance_path).unwrap()).unwrap();
        assert_eq!(saved["request"]["mode"], "full");
        assert!(saved["request"].get("max_edge").is_none());
        assert!(saved["request"].get("rust_exif").is_none());
        assert_eq!(saved["exif_inherited"], true);
        assert_eq!(fs::read(&pair).unwrap(), original);
        let preset = crate::presets::get_presets().remove(0);
        let contract = full_developed_result(&first, &preset);
        assert_eq!(contract["preview"], false);
        assert_eq!(contract["exif_inherited"], true);
        assert_eq!(contract["width"], 80);
        assert_eq!(
            contract["filename"],
            Path::new(&first.output_path)
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
        );
        assert!(contract["recipe_filename"]
            .as_str()
            .unwrap()
            .ends_with(".json"));
        assert_eq!(contract["params_hash"].as_str().unwrap().len(), 10);
        assert!(contract["output_url"]
            .as_str()
            .unwrap()
            .starts_with("asset://localhost/%2F"));
        let cached = run();
        assert!(cached.cache_hit && cached.exif_inherited && !cached.preview);
        assert_eq!(calls(bin.path()), 1);
        // TIFF のパディングだけが変わっても別のペアとして鍵を更新する。
        let mut changed = original.clone();
        let n = changed.len();
        changed[n - 3] ^= 1;
        fs::write(&pair, changed).unwrap();
        let updated = run();
        assert!(updated.exif_inherited && !updated.cache_hit);
        assert_ne!(first.params_hash, updated.params_hash);
        for bytes in [b"not a JPEG".as_slice(), plain.as_slice()] {
            fs::write(&pair, bytes).unwrap();
            let result = run();
            assert!(!result.exif_inherited && !result.cache_hit);
            assert_eq!(fs::read(&result.output_path).unwrap(), plain);
            assert!(run().cache_hit);
        }
        fs::remove_file(&pair).unwrap();
        let missing = run();
        assert!(!missing.exif_inherited && !missing.cache_hit);
        assert_eq!(fs::read(&missing.output_path).unwrap(), plain);
        // preview の結果に EXIF を付けない。
        fs::write(&pair, original).unwrap();
        let preview = develop(
            app.path(),
            &cli,
            "DSC00001",
            DevelopMode::Preview,
            Some(&adjustments),
            &slot,
            Duration::from_secs(5),
        )
        .unwrap();
        assert!(preview.preview && !preview.exif_inherited);
        assert_eq!(fs::read(preview.output_path).unwrap(), plain);
    }

    #[test]
    fn cache_key_changes_when_cli_binary_changes_without_version_bump() {
        // --version の応答（cli_version / macos_version）は同じで、実装だけ違う 2 つの CLI。
        let first = TempDir::new("binary-a");
        let second = TempDir::new("binary-b");
        let cli_a = mock_cli(first.path(), MOCK_OK);
        let cli_b = mock_cli(second.path(), &format!("# rebuilt\n{MOCK_OK}"));
        let version_a = cli_version(&cli_a).unwrap();
        let version_b = cli_version(&cli_b).unwrap();
        assert_eq!(version_a.fingerprint, version_b.fingerprint);
        assert_ne!(version_a.binary_sha256, version_b.binary_sha256);
        let request = base_request(
            Path::new("/abs/DSC00001.ARW"),
            DevelopMode::Preview,
            Some(&sample_adjustments()),
        );
        assert_ne!(
            cache_key(&request, &version_a, 100, 5),
            cache_key(&request, &version_b, 100, 5)
        );
    }

    #[test]
    fn develop_with_mock_cli_sends_contract_and_caches() {
        let (app_data, source) = fake_app("mock", &["DSC00001"]);
        let bin = TempDir::new("mock-bin");
        let cli = mock_cli(bin.path(), MOCK_OK);
        let slot = DevelopSlot::new(MAX_WAITING);
        let adjustments = sample_adjustments();
        let arw = source.path().join("DSC00001.ARW");
        let before = fs::read(&arw).unwrap();
        let coreimage_dir = app_data.path().join(OUTPUT_DIR_NAME);
        fs::create_dir_all(&coreimage_dir).unwrap();

        let first = develop(
            app_data.path(),
            &cli,
            "DSC00001",
            DevelopMode::Preview,
            Some(&adjustments),
            &slot,
            DEVELOP_TIMEOUT,
        )
        .unwrap();
        assert!(!first.cache_hit);
        assert_eq!(calls(bin.path()), 1);
        assert_eq!((first.width, first.height), (2400, 1600));
        assert_eq!(first.params_hash.len(), 16);
        assert_eq!(first.cli_version, "0.6.0");
        assert_eq!(first.macos_version, "27.0.1");
        assert_eq!(first.decoder_version, "9");
        assert_eq!(first.adjustments, adjustments);
        assert_eq!(first.applied["profile"], json!("apple_default"));
        // CLI 0.6.0（#81）: highlight_recovery は写像で効くので ignored は空、実効値がそのまま届く。
        assert_eq!(first.adjustments_applied["ignored"], json!([]));
        assert_eq!(
            first.adjustments_applied["highlight_recovery"]["effective_exposure"],
            json!(-0.7)
        );
        assert_eq!(first.source["iso"], json!(3200));
        assert_eq!(first.elapsed_ms["total"], json!(3.0));

        // 出力は coreimage/preview/<ID>_<hash16>.{jpg,json}。
        let preview_dir = coreimage_dir.canonicalize().unwrap().join("preview");
        let output = PathBuf::from(&first.output_path);
        assert_eq!(output.parent(), Some(preview_dir.as_path()));
        assert_eq!(
            output.file_name().and_then(|n| n.to_str()),
            Some(format!("DSC00001_{}.jpg", first.params_hash).as_str())
        );
        assert_eq!(
            PathBuf::from(&first.provenance_path),
            output.with_extension("json")
        );

        // CLI に渡したリクエスト（来歴 = 応答全文に mock が埋め込んだもの）。
        let provenance: Value =
            serde_json::from_str(&fs::read_to_string(&first.provenance_path).unwrap()).unwrap();
        let request = &provenance["request"];
        assert_eq!(
            request["settings"],
            json!({"profile": "apple_default", "lens_correction": true})
        );
        assert_eq!(request["adjustments"], adjustments);
        assert_eq!(request["max_edge"], json!(2400));
        assert_eq!(request["decoder_version"], json!("9"));
        assert_eq!(request["format"], json!("jpeg"));
        assert_eq!(request["mode"], json!("preview"));
        assert_eq!(request["output"], json!(first.output_path));
        assert_eq!(
            request["input"],
            json!(arw.canonicalize().unwrap().to_string_lossy())
        );

        // 2 回目はキャッシュヒット（CLI を呼ばない）。フォルダの列挙・config の読み直しもしない（#94）。
        let (dirs_before, configs_before) = (read_dir_calls(), config_reads());
        let second = develop(
            app_data.path(),
            &cli,
            "DSC00001",
            DevelopMode::Preview,
            Some(&adjustments),
            &slot,
            DEVELOP_TIMEOUT,
        )
        .unwrap();
        assert!(second.cache_hit);
        assert_eq!(calls(bin.path()), 1);
        assert_eq!(
            read_dir_calls(),
            dirs_before,
            "キャッシュヒットで列挙しない"
        );
        assert_eq!(
            config_reads(),
            configs_before,
            "キャッシュヒットで config を読まない"
        );
        assert_eq!(second.output_path, first.output_path);
        assert_eq!(second.params_hash, first.params_hash);
        assert_eq!(second.adjustments_applied, first.adjustments_applied);

        // adjustments を 1 つ変えると別ハッシュで現像し直す。
        let mut changed = adjustments.clone();
        changed["noise_reduction"] = json!("full");
        let third = develop(
            app_data.path(),
            &cli,
            "DSC00001",
            DevelopMode::Preview,
            Some(&changed),
            &slot,
            DEVELOP_TIMEOUT,
        )
        .unwrap();
        assert!(!third.cache_hit);
        assert_ne!(third.params_hash, first.params_hash);
        assert_eq!(calls(bin.path()), 2);
        assert!(output.is_file(), "前の組は上限内なので残る");

        // 原本不変。
        assert_eq!(fs::read(&arw).unwrap(), before);
    }

    #[test]
    fn develop_reports_cli_error_and_busy() {
        let (app_data, _source) = fake_app("mockfail", &["DSC00002"]);
        let bin = TempDir::new("mockfail-bin");
        let cli = mock_cli(bin.path(), MOCK_FAIL);
        let adjustments = sample_adjustments();
        let slot = DevelopSlot::new(0);
        let run = |slot: &DevelopSlot| {
            develop(
                app_data.path(),
                &cli,
                "DSC00002",
                DevelopMode::Preview,
                Some(&adjustments),
                slot,
                DEVELOP_TIMEOUT,
            )
        };
        let error = run(&slot).unwrap_err();
        assert_eq!(error.code, "RENDER_FAILED");
        assert_eq!(error.message, "boom");
        let preview_dir = app_data.path().join(OUTPUT_DIR_NAME).join("preview");
        assert!(
            fs::read_dir(&preview_dir).unwrap().next().is_none(),
            "失敗時は来歴を書かない"
        );

        // 枠が埋まっていて待ちも 0 なら、CLI を呼ばずに BUSY。
        let before = calls(bin.path());
        let held = slot.acquire().unwrap();
        assert_eq!(run(&slot).unwrap_err().code, "BUSY");
        assert_eq!(calls(bin.path()), before);
        drop(held);

        // 不正な capture_id は CLI もファイルシステムも触る前に落ちる。
        let error = develop(
            app_data.path(),
            &cli,
            "../DSC00002",
            DevelopMode::Preview,
            Some(&adjustments),
            &slot,
            DEVELOP_TIMEOUT,
        )
        .unwrap_err();
        assert_eq!(error.code, "INVALID_CAPTURE_ID");
    }

    #[test]
    fn develop_lru_keeps_new_entry() {
        // prune は develop の最後で走る。上限は定数なので、ここでは「いま書いた組が残り、
        // 名前の形が違うファイルに触れない」ことだけを見る（件数・容量の挙動は
        // prune_cache_removes_oldest_pairs_to_80_percent）。
        let (app_data, _source) = fake_app("mocklru", &["DSC00003"]);
        let bin = TempDir::new("mocklru-bin");
        let cli = mock_cli(bin.path(), MOCK_OK);
        let (_, preview_dir) = prepare_output_dirs(app_data.path(), DevelopMode::Preview).unwrap();
        let foreign = preview_dir.join("README.txt");
        touch_file(&foreign);
        let result = develop(
            app_data.path(),
            &cli,
            "DSC00003",
            DevelopMode::Preview,
            Some(&sample_adjustments()),
            &DevelopSlot::new(MAX_WAITING),
            DEVELOP_TIMEOUT,
        )
        .unwrap();
        assert!(Path::new(&result.output_path).is_file());
        assert!(foreign.exists());
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

    /// 実 ARW のフォルダ（Swift の `GEOFRAME_CI_SMOKE_ARW` と同じ流儀で差し替え可能）。
    const REAL_ROOT_ENV: &str = "GEOFRAME_CI_TEST_ARW_ROOT";
    const REAL_ROOT_DEFAULT: &str = "/Volumes/Backup/100MSDCF";

    /// `cli_version` が 0.3.0 以上か（adjustments を受け付ける版か）。
    fn supports_adjustments(cli_version: &str) -> bool {
        let mut parts = cli_version
            .split('.')
            .map(|part| part.parse::<u64>().unwrap_or(0));
        let major = parts.next().unwrap_or(0);
        let minor = parts.next().unwrap_or(0);
        major > 0 || minor >= 3
    }

    #[test]
    fn supports_adjustments_parses_versions() {
        assert!(!supports_adjustments("0.2.0"));
        assert!(supports_adjustments("0.3.0"));
        assert!(supports_adjustments("0.10.1"));
        assert!(supports_adjustments("1.0.0"));
        assert!(!supports_adjustments("unknown"));
    }

    /// 実 ARW での結合テスト（Tauri のコマンドと同じ `develop` を通す）。
    ///
    /// `GEOFRAME_CI_TEST_ARW_ROOT`（既定 `/Volumes/Backup/100MSDCF`）に ARW が無いか、sidecar が
    /// 無ければ skip。出力は一時ディレクトリの偽 app_data_dir へ書き、本物の
    /// `~/Library/Application Support/com.geoframe.rawassist/` には触れない。
    ///
    /// - 既定リクエスト（adjustments 無し）は 0.2.0 でも通る: apple_default + レンズ補正 ON、
    ///   長辺 2400、`coreimage/preview/` への出力、キャッシュヒット、原本不変。
    /// - adjustments 付きは CLI 0.3.0 以上のときだけ現像まで確かめる。0.2.0 では
    ///   `INVALID_REQUEST`（unknown request key）で拒否されることだけを確かめる。
    #[test]
    fn develops_real_arw_when_available() {
        let Some(sidecar) = built_sidecar() else {
            eprintln!("skip: src-tauri/binaries/geoframe-ci-develop-* がありません");
            return;
        };
        let root = std::env::var(REAL_ROOT_ENV).unwrap_or_else(|_| REAL_ROOT_DEFAULT.to_string());
        let root = PathBuf::from(root);
        let mut ids: Vec<String> = fs::read_dir(&root)
            .map(|entries| {
                entries
                    .flatten()
                    .filter_map(|entry| entry.file_name().into_string().ok())
                    .filter_map(|name| {
                        entry_pattern()
                            .captures(&name)
                            .filter(|found| found[2].eq_ignore_ascii_case("ARW"))
                            .map(|found| found[1].to_string())
                    })
                    .collect()
            })
            .unwrap_or_default();
        ids.sort();
        ids.dedup();
        ids.truncate(2);
        if ids.is_empty() {
            eprintln!(
                "skip: {} に実 ARW がありません（{REAL_ROOT_ENV} で指定）",
                root.display()
            );
            return;
        }
        let app_data = TempDir::new("appdata");
        fs::write(
            app_data.path().join("config.json"),
            json!({"config_version": 1, "source_root": root}).to_string(),
        )
        .unwrap();
        let slot = DevelopSlot::new(MAX_WAITING);
        let version = cli_version(&sidecar).expect("--version");
        let adjustments = sample_adjustments();
        let output_dir = app_data
            .path()
            .canonicalize()
            .unwrap()
            .join(OUTPUT_DIR_NAME)
            .join("preview");

        for capture_id in &ids {
            let input = resolve_arw(&root, capture_id).unwrap();
            let before = fs::metadata(&input).unwrap();
            let run = |adjustments: Option<&Value>| {
                develop(
                    app_data.path(),
                    &sidecar,
                    capture_id,
                    DevelopMode::Preview,
                    adjustments,
                    &slot,
                    DEVELOP_TIMEOUT,
                )
            };

            // 既定リクエスト（0.2.0 でも通る範囲）。
            let first = run(None).unwrap_or_else(|error| panic!("{capture_id}: {error:?}"));
            assert!(!first.cache_hit);
            assert_eq!(first.decoder_version, "9");
            assert_eq!(first.width.max(first.height), 2400, "{capture_id} の長辺");
            assert_eq!(first.applied["profile"], json!("apple_default"));
            if first.applied["lens_correction_supported"] == json!(true) {
                assert_eq!(first.applied["lens_correction_enabled"], json!(true));
            }
            assert_eq!(first.cli_version, version.cli_version);
            let output = PathBuf::from(&first.output_path);
            assert_eq!(output.parent(), Some(output_dir.as_path()));
            assert!(output.is_file());
            assert!(Path::new(&first.provenance_path).is_file(), "来歴 JSON");
            assert!(first.elapsed_ms.get("total").is_some());

            let second = run(None).unwrap();
            assert!(second.cache_hit, "2 回目はキャッシュヒット");
            assert_eq!(second.output_path, first.output_path);
            assert_eq!((second.width, second.height), (first.width, first.height));

            // adjustments 付き。
            if supports_adjustments(&version.cli_version) {
                let with_adj = run(Some(&adjustments)).unwrap();
                assert!(!with_adj.cache_hit);
                assert_ne!(with_adj.params_hash, first.params_hash);
                assert_eq!(with_adj.width.max(with_adj.height), 2400);
                // CLI 0.6.0（#81）から ignored は空配列（hr は exposure / shadow_lift へ写像して加算）。
                assert_eq!(with_adj.adjustments_applied["ignored"], json!([]));
                let hr = &with_adj.adjustments_applied["highlight_recovery"];
                let expected_hr = adjustments["highlight_recovery"].as_f64().unwrap();
                assert_eq!(hr["value"].as_f64(), Some(expected_hr));
                assert!(with_adj.adjustments_applied["post"].is_object());
                assert!(run(Some(&adjustments)).unwrap().cache_hit);
                let mut changed = adjustments.clone();
                changed["sharpness"] = json!(1.5);
                let other = run(Some(&normalize_adjustments(&changed).unwrap())).unwrap();
                assert_ne!(other.params_hash, with_adj.params_hash);
            } else {
                eprintln!(
                    "skip: CLI {} は adjustments 非対応（0.3.0 以上で現像まで確認）",
                    version.cli_version
                );
                let error = run(Some(&adjustments)).unwrap_err();
                assert_eq!(error.code, "INVALID_REQUEST", "{error:?}");
            }

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
            Some(&adjustments),
            &slot,
            DEVELOP_TIMEOUT,
        )
        .unwrap_err();
        assert_eq!(error.code, "ARW_NOT_FOUND");
    }
}
