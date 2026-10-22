//! 現像元フォルダ設定。Python `local_raw/config.py` と同じ config.json を扱う。
use serde::Serialize;
use serde_json::{Map, Value};
use std::collections::HashSet;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tauri::Manager;

const CONFIG_VERSION: u64 = 1;
static WRITE_LOCK: Mutex<()> = Mutex::new(());
const PERMISSION_MESSAGE: &str = "フォルダを読み取れません。システム設定 > プライバシーとセキュリティ > ファイルとフォルダ から GeoFrame RAW Assist（アプリ本体）にアクセスを許可してください。";

#[derive(Clone, Serialize)]
pub struct SourceFolderInfo {
    path: String,
    display_path: String,
    is_default: bool,
    exists: bool,
    readable: bool,
    pairs: u64,
    jpg_only: u64,
    arw_only: u64,
    total_bytes: u64,
}

#[derive(Clone, Serialize)]
pub struct SourceFolderError {
    code: &'static str,
    reason: &'static str,
    message: String,
}

fn error(reason: &'static str, message: impl Into<String>) -> SourceFolderError {
    SourceFolderError {
        code: reason,
        reason,
        message: message.into(),
    }
}

/// Python `_read_config`: 欠損・不正 JSON・非オブジェクトは空設定。
fn read_config(path: &Path) -> Map<String, Value> {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

pub fn read_source_root(app_data_dir: &Path) -> Result<PathBuf, crate::coreimage::CoreImageError> {
    let value = read_config(&app_data_dir.join("config.json"));
    match value.get("source_root").and_then(Value::as_str) {
        Some(root) if !root.is_empty() && Path::new(root).is_absolute() => Ok(PathBuf::from(root)),
        _ => Err(crate::coreimage::CoreImageError::new(
            "SOURCE_ROOT_NOT_CONFIGURED",
            "現像元フォルダが未設定です（先にアプリで現像元フォルダを選んでください）",
        )),
    }
}

fn selected_root(app_data_dir: &Path) -> PathBuf {
    read_config(&app_data_dir.join("config.json"))
        .get("source_root")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && Path::new(s).is_absolute())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .join("sample/100MSDCF")
        })
}

fn info(root: &Path) -> SourceFolderInfo {
    let resolved = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let metadata = fs::metadata(&resolved).ok();
    let exists = metadata.as_ref().is_some_and(|m| m.is_dir());
    let readable = exists && fs::read_dir(&resolved).is_ok();
    let mut jpg = HashSet::new();
    let mut arw = HashSet::new();
    let mut total_bytes = 0;
    if readable {
        if let Ok(entries) = fs::read_dir(&resolved) {
            for entry in entries.flatten() {
                let path = entry.path();
                if !path.is_file() {
                    continue;
                }
                let ext = path
                    .extension()
                    .and_then(|s| s.to_str())
                    .unwrap_or_default();
                if ext.eq_ignore_ascii_case("jpg") {
                    jpg.insert(path.file_stem().unwrap_or_default().to_os_string());
                    total_bytes += path.metadata().map(|m| m.len()).unwrap_or(0);
                } else if ext.eq_ignore_ascii_case("arw") {
                    arw.insert(path.file_stem().unwrap_or_default().to_os_string());
                    total_bytes += path.metadata().map(|m| m.len()).unwrap_or(0);
                }
            }
        }
    }
    let pairs = jpg.intersection(&arw).count() as u64;
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let project = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let display_path = resolved
        .strip_prefix(project)
        .ok()
        .map(|p| p.display().to_string())
        .or_else(|| {
            home.as_ref()
                .and_then(|h| resolved.strip_prefix(h).ok())
                .map(|p| {
                    if p.as_os_str().is_empty() {
                        "~".into()
                    } else {
                        format!("~/{}", p.display())
                    }
                })
        })
        .unwrap_or_else(|| resolved.display().to_string());
    SourceFolderInfo {
        path: resolved.display().to_string(),
        display_path,
        is_default: false,
        exists,
        readable,
        pairs,
        jpg_only: jpg.len() as u64 - pairs,
        arw_only: arw.len() as u64 - pairs,
        total_bytes,
    }
}

fn validate(path: &str, analysis_running: bool) -> Result<PathBuf, SourceFolderError> {
    validate_with(path, analysis_running, |root| fs::read_dir(root).is_ok())
}

fn validate_with(
    path: &str,
    analysis_running: bool,
    readable: impl Fn(&Path) -> bool,
) -> Result<PathBuf, SourceFolderError> {
    if analysis_running {
        return Err(error(
            "analysis_running",
            "解析の実行中はフォルダを変更できません。完了後にもう一度お試しください。",
        ));
    }
    if path.trim().is_empty() {
        return Err(error("invalid", "フォルダのパスを指定してください。"));
    }
    let candidate = Path::new(path.trim());
    if !candidate.is_absolute() {
        return Err(error("invalid", "フォルダは絶対パスで指定してください。"));
    }
    let metadata = match fs::metadata(candidate) {
        Ok(m) => m,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            return Err(error("not_found", "指定されたフォルダが見つかりません。"))
        }
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied => {
            return Err(error("permission_denied", PERMISSION_MESSAGE))
        }
        Err(_) => return Err(error("invalid", "フォルダのパスが不正です。")),
    };
    if !metadata.is_dir() {
        return Err(error(
            "not_directory",
            "指定されたパスはフォルダではありません。",
        ));
    }
    let resolved = candidate
        .canonicalize()
        .map_err(|_| error("invalid", "フォルダのパスが不正です。"))?;
    if !readable(&resolved) {
        return Err(error("permission_denied", PERMISSION_MESSAGE));
    }
    Ok(resolved)
}

fn write_config(path: &Path, source: &Path) -> io::Result<()> {
    let mut config = read_config(path);
    config.insert("config_version".into(), Value::from(CONFIG_VERSION));
    config.insert(
        "source_root".into(),
        Value::String(source.display().to_string()),
    );
    let parent = path.parent().unwrap();
    fs::create_dir_all(parent)?;
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temp = parent.join(format!("config.{}-{nonce}.tmp", std::process::id()));
    let result = (|| {
        fs::write(
            &temp,
            format!("{}\n", serde_json::to_string_pretty(&config).unwrap()),
        )?;
        fs::rename(&temp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(temp);
    }
    result
}

#[tauri::command]
pub fn get_source_folder(app: tauri::AppHandle) -> SourceFolderInfo {
    match app.path().app_data_dir() {
        Ok(root) => info(&selected_root(&root)),
        Err(_) => info(
            &Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .join("sample/100MSDCF"),
        ),
    }
}

#[tauri::command]
pub fn set_source_folder(
    app: tauri::AppHandle,
    analysis: tauri::State<'_, crate::analysis::AnalysisState>,
    path: String,
) -> Result<SourceFolderInfo, SourceFolderError> {
    let root = validate(&path, analysis.is_running())?;
    let app_data = app
        .path()
        .app_data_dir()
        .map_err(|e| error("invalid", e.to_string()))?;
    let _guard = WRITE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    write_config(&app_data.join("config.json"), &root)
        .map_err(|e| error("invalid", format!("設定を保存できません: {e}")))?;
    Ok(info(&root))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn corrupt_config_is_empty_and_write_preserves_unknown_keys_and_order() {
        let dir = std::env::temp_dir().join(format!("source-folder-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let file = dir.join("config.json");
        fs::write(&file, "{").unwrap();
        assert!(read_config(&file).is_empty());
        fs::write(
            &file,
            r#"{"custom":7,"config_version":1,"source_root":"old"}"#,
        )
        .unwrap();
        let root = dir.canonicalize().unwrap();
        write_config(&file, &root).unwrap();
        let text = fs::read_to_string(&file).unwrap();
        assert!(text.find("custom").unwrap() < text.find("config_version").unwrap());
        assert_eq!(serde_json::from_str::<Value>(&text).unwrap()["custom"], 7);
        assert_eq!(
            serde_json::from_str::<Value>(&text).unwrap()["config_version"],
            1
        );
        let _ = fs::remove_dir_all(dir);
    }
    #[test]
    fn validation_reasons() {
        let dir = std::env::temp_dir().join(format!("source-validation-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let file = dir.join("file");
        fs::write(&file, "x").unwrap();
        assert_eq!(validate("", false).unwrap_err().reason, "invalid");
        assert_eq!(validate("relative", false).unwrap_err().reason, "invalid");
        assert_eq!(
            validate(&dir.join("missing").display().to_string(), false)
                .unwrap_err()
                .reason,
            "not_found"
        );
        assert_eq!(
            validate(&file.display().to_string(), false)
                .unwrap_err()
                .reason,
            "not_directory"
        );
        assert_eq!(
            validate(&dir.display().to_string(), true)
                .unwrap_err()
                .reason,
            "analysis_running"
        );
        assert_eq!(
            validate_with(&dir.display().to_string(), false, |_| false)
                .unwrap_err()
                .reason,
            "permission_denied"
        );
        assert!(validate_with(&dir.display().to_string(), false, |_| false)
            .unwrap_err()
            .message
            .contains("GeoFrame RAW Assist"));
        assert!(validate(&dir.display().to_string(), false).is_ok());
        let _ = fs::remove_dir_all(dir);
    }
}
