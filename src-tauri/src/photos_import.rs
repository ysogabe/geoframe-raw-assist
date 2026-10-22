//! APPDATA/export の書き出し済み JPEG を Apple「写真」へ取り込む。
use serde_json::{json, Value};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};
use tauri::Manager;

const TIMEOUT: Duration = Duration::from_secs(180);
const ALBUM_PREFIX: &str = "GeoFrame RAW Assist";
const STDERR_LIMIT: usize = 500;
const PERMISSION_HINT: &str = "「システム設定 > プライバシーとセキュリティ > オートメーション」で、GeoFrame RAW Assist（アプリ本体）に「写真」の操作を許可してください。";
const PERMISSION_SIGNATURES: &[&str] = &[
    "-1743",
    "-1728",
    "Not authorized",
    "not allowed",
    "許可されていません",
];

fn album_name(batch: &str, requested: Option<&str>) -> Result<String, String> {
    let Some(requested) = requested else {
        return Ok(format!("{ALBUM_PREFIX} {batch}"));
    };
    let name = requested.trim();
    if name.is_empty() {
        return Err("アルバム名を指定してください。".into());
    }
    if name.chars().count() > 120 {
        return Err("アルバム名は120文字以内で指定してください。".into());
    }
    if name.contains(['\n', '\r', '\0']) {
        return Err("アルバム名に改行は使用できません。".into());
    }
    Ok(name.to_string())
}

fn exported_jpegs(data: &Path, batch: &str) -> Result<Vec<PathBuf>, String> {
    let batch = crate::export::normalize_batch_name(batch)?;
    let data = data
        .canonicalize()
        .map_err(|e| format!("書き出しフォルダを確認できません: {e}"))?;
    let export_root = data.join("export");
    let root_meta = fs::symlink_metadata(&export_root).map_err(|_| {
        "書き出し済みのフォルダが見つかりません。先に書き出しを実行してください。".to_string()
    })?;
    if !root_meta.file_type().is_dir()
        || export_root
            .canonicalize()
            .map_err(|e| e.to_string())?
            .parent()
            != Some(data.as_path())
    {
        return Err("書き出し先が不正です。".into());
    }
    let dir = export_root.join(&batch);
    let meta = fs::symlink_metadata(&dir).map_err(|_| {
        "書き出し済みのフォルダが見つかりません。先に書き出しを実行してください。".to_string()
    })?;
    let resolved = dir.canonicalize().map_err(|e| e.to_string())?;
    if !meta.file_type().is_dir() || resolved.parent() != Some(export_root.as_path()) {
        return Err("書き出し先が不正です。".into());
    }
    let mut files = Vec::new();
    for entry in
        fs::read_dir(&resolved).map_err(|e| format!("書き出しフォルダを読めません: {e}"))?
    {
        let path = entry.map_err(|e| e.to_string())?.path();
        let meta = fs::symlink_metadata(&path).map_err(|e| e.to_string())?;
        if path
            .extension()
            .and_then(|s| s.to_str())
            .is_some_and(|s| s.eq_ignore_ascii_case("jpg"))
        {
            let canonical = path.canonicalize().map_err(|e| e.to_string())?;
            if !meta.file_type().is_file() || canonical.parent() != Some(resolved.as_path()) {
                return Err("取り込み対象JPEGが書き出しフォルダ外を参照しています。".into());
            }
            files.push(canonical);
        }
    }
    files.sort();
    if files.is_empty() {
        return Err("取り込めるJPEGが書き出しフォルダにありません。".into());
    }
    Ok(files)
}

fn script_path(resource_dir: &Path) -> PathBuf {
    let bundled = resource_dir.join("photos_import.applescript");
    if bundled.is_file() {
        return bundled;
    }
    // tauri dev では resource_dir がアプリ bundle を指さないため、同梱元を解決する。
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../scripts/photos_import.applescript")
}

fn build_args(script: &Path, album: &str, files: &[PathBuf]) -> Vec<String> {
    std::iter::once(script.to_string_lossy().into_owned())
        .chain(std::iter::once(album.to_string()))
        .chain(files.iter().map(|p| p.to_string_lossy().into_owned()))
        .collect()
}

fn error(message: String, stderr: Option<&str>) -> Value {
    let mut value = json!({"status":"error", "error":message});
    if let Some(stderr) = stderr.filter(|s| !s.is_empty()) {
        value["stderr"] = json!(stderr.chars().take(STDERR_LIMIT).collect::<String>());
    }
    value
}

fn permission_error(stderr: &str) -> bool {
    PERMISSION_SIGNATURES.iter().any(|s| stderr.contains(s))
}

fn import_with(
    data: &Path,
    script: &Path,
    batch: &str,
    requested_album: Option<&str>,
    mut run: impl FnMut(&[String], Duration) -> Result<Output, String>,
) -> Result<Value, String> {
    let files = exported_jpegs(data, batch)?;
    let normalized_batch = crate::export::normalize_batch_name(batch)?;
    let album = album_name(&normalized_batch, requested_album)?;
    if !script.is_file() {
        return Ok(error("取り込み用スクリプトが見つかりません。".into(), None));
    }
    let args = build_args(script, &album, &files);
    let output = match run(&args, TIMEOUT) {
        Ok(o) => o,
        Err(e) if e == "timeout" => {
            return Ok(error(
                format!("「写真」への取り込みが時間内に終わりませんでした。{PERMISSION_HINT}"),
                None,
            ))
        }
        Err(e) if e == "not_found" => {
            return Ok(error(
                "osascript が見つかりません。macOS 上で実行してください。".into(),
                None,
            ))
        }
        Err(e) => {
            return Ok(error(
                format!("「写真」への取り込みを開始できませんでした: {e}"),
                None,
            ))
        }
    };
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        let message = if permission_error(&stderr) {
            format!("「写真」の操作が許可されていません。{PERMISSION_HINT}")
        } else {
            format!("「写真」への取り込みに失敗しました。{PERMISSION_HINT}")
        };
        return Ok(error(message, Some(stderr.trim())));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let imported = stdout.trim().parse::<usize>().unwrap_or(files.len());
    Ok(json!({"status":"ok", "imported":imported, "album":album}))
}

fn run_osascript(args: &[String], timeout: Duration) -> Result<Output, String> {
    let mut child = Command::new("osascript")
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                "not_found".into()
            } else {
                e.to_string()
            }
        })?;
    let stdout = child.stdout.take().ok_or("stdout を取得できません")?;
    let stderr = child.stderr.take().ok_or("stderr を取得できません")?;
    let out_reader = thread::spawn(move || {
        let mut b = Vec::new();
        let _ = stdout.bytes().collect::<Result<Vec<_>, _>>().map(|v| b = v);
        b
    });
    let err_reader = thread::spawn(move || {
        let mut b = Vec::new();
        let _ = stderr.bytes().collect::<Result<Vec<_>, _>>().map(|v| b = v);
        b
    });
    let started = Instant::now();
    loop {
        if child.try_wait().map_err(|e| e.to_string())?.is_some() {
            break;
        }
        if started.elapsed() >= timeout {
            let _ = child.kill();
            let _ = child.wait();
            return Err("timeout".into());
        }
        thread::sleep(Duration::from_millis(100));
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    Ok(Output {
        status,
        stdout: out_reader.join().unwrap_or_default(),
        stderr: err_reader.join().unwrap_or_default(),
    })
}

#[tauri::command]
pub async fn import_to_photos(
    app: tauri::AppHandle,
    batch_name: String,
    album: Option<String>,
) -> Result<Value, String> {
    let data = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let resource_dir = app.path().resource_dir().map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || {
        let script = script_path(&resource_dir);
        import_with(&data, &script, &batch_name, album.as_deref(), run_osascript)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::{
        os::unix::{fs::symlink, process::ExitStatusExt},
        process::ExitStatus,
    };
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    fn fixture() -> (PathBuf, PathBuf) {
        let d = std::env::temp_dir().join(format!(
            "photos-import-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(d.join("export/holiday")).unwrap();
        fs::write(d.join("export/holiday/DSC00001.JPG"), b"jpeg").unwrap();
        fs::write(d.join("export/holiday/manifest.json"), b"{}").unwrap();
        let script = d.join("photos_import.applescript");
        fs::write(&script, "on run argv\nend run").unwrap();
        (d, script)
    }
    fn output(code: i32, stderr: &[u8], stdout: &[u8]) -> Output {
        Output {
            status: ExitStatus::from_raw(code << 8),
            stdout: stdout.into(),
            stderr: stderr.into(),
        }
    }
    #[test]
    fn builds_array_args_and_album_and_limits_scope() {
        let (d, script) = fixture();
        let mut seen = Vec::new();
        let result = import_with(&d, &script, " holiday ", None, |args, timeout| {
            seen = args.to_vec();
            assert_eq!(timeout, TIMEOUT);
            Ok(output(0, b"", b"1"))
        })
        .unwrap();
        assert_eq!(result["album"], "GeoFrame RAW Assist holiday");
        assert_eq!(
            seen,
            vec![
                script.to_string_lossy().to_string(),
                "GeoFrame RAW Assist holiday".into(),
                d.join("export/holiday/DSC00001.JPG")
                    .canonicalize()
                    .unwrap()
                    .to_string_lossy()
                    .to_string()
            ]
        );
        assert!(!seen[1].contains("\n"));
        fs::remove_dir_all(d).unwrap();
    }
    #[test]
    fn rejects_bad_batch_and_symlink_jpeg() {
        let (d, script) = fixture();
        for batch in ["../holiday", "holiday/child", "", "a..b"] {
            assert!(exported_jpegs(&d, batch).is_err());
        }
        let outside = d.join("outside.jpg");
        fs::write(&outside, b"x").unwrap();
        let jpeg = d.join("export/holiday/linked.jpg");
        symlink(&outside, &jpeg).unwrap();
        assert!(exported_jpegs(&d, "holiday").is_err());
        fs::remove_dir_all(d).unwrap();
        let _ = script;
    }
    #[test]
    fn maps_automation_failure() {
        let (d, script) = fixture();
        let result = import_with(&d, &script, "holiday", None, |_, _| {
            Ok(output(
                1,
                b"Not authorized to send Apple events (-1743)",
                b"",
            ))
        })
        .unwrap();
        assert_eq!(result["status"], "error");
        assert!(result["error"]
            .as_str()
            .unwrap()
            .contains("GeoFrame RAW Assist（アプリ本体）"));
        assert_eq!(
            result["stderr"],
            "Not authorized to send Apple events (-1743)"
        );
        fs::remove_dir_all(d).unwrap();
    }
}
