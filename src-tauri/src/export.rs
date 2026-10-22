//! Rust 書き出し。画像は一切加工せず、検証後にバイト列をコピーする。
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex,
    },
};
use tauri::Manager;

static EXPORT_LOCK: Mutex<()> = Mutex::new(());
static NONCE: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Deserialize)]
pub struct ExportItem {
    capture_id: String,
    variant: String,
    developed_filename: Option<String>,
}

fn normalize_batch_name(raw: &str) -> Result<String, String> {
    let name = raw.trim();
    if name.is_empty() || name.chars().count() > 64 {
        return Err("バッチ名は1〜64文字で指定してください。".into());
    }
    if name.contains(['/', '\\', '\0']) || name.contains("..") {
        return Err("バッチ名にパス区切りは使用できません。".into());
    }
    Ok(name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect())
}

fn io_error(e: std::io::Error) -> String {
    format!("書き出しファイルを処理できません: {e}")
}

/// ディレクトリ自体の symlink も許可しない。APPDATA は呼び出し元で canonicalize 済み。
fn real_dir(parent: &Path, name: &str) -> Result<PathBuf, String> {
    let path = parent.join(name);
    match fs::create_dir(&path) {
        Ok(()) => (),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(e) => return Err(io_error(e)),
    }
    let meta = fs::symlink_metadata(&path).map_err(io_error)?;
    if !meta.file_type().is_dir() || path.canonicalize().map_err(io_error)?.parent() != Some(parent)
    {
        return Err("許可したフォルダの外には書き出せません。".into());
    }
    Ok(path)
}

/// 欠損のみ skipped。リンク・範囲外・通常ファイル以外は入力エラー。
fn source_file(root: &Path, name: &str) -> Result<Option<PathBuf>, String> {
    let path = root.join(name);
    let meta = match fs::symlink_metadata(&path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(io_error(e)),
    };
    let resolved = path.canonicalize().map_err(io_error)?;
    if !meta.file_type().is_file() || resolved.parent() != Some(root) {
        return Err("複製元が許可したフォルダの通常ファイルではありません。".into());
    }
    Ok(Some(resolved))
}

fn sha256(path: &Path) -> Result<String, String> {
    let mut file = fs::File::open(path).map_err(io_error)?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let n = file.read(&mut buffer).map_err(io_error)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

/// create_new の一時ファイル + rename。既存の出力 symlink を追わない。
fn atomic_write(
    destination: &Path,
    write: impl FnOnce(&mut fs::File) -> Result<(), String>,
) -> Result<(), String> {
    let temporary = destination.with_file_name(format!(
        ".export-{}-{}.tmp",
        std::process::id(),
        NONCE.fetch_add(1, Ordering::Relaxed)
    ));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(io_error)?;
    let result =
        write(&mut file).and_then(|()| fs::rename(&temporary, destination).map_err(io_error));
    let _ = fs::remove_file(temporary);
    result
}

fn developed(
    data: &Path,
    root: &Path,
    id: &str,
    filename: &str,
) -> Result<Option<(PathBuf, Value)>, String> {
    if filename.contains(['/', '\\', '\0'])
        || filename.contains("..")
        || !(filename.starts_with(&format!("{id}_")) || filename.starts_with(&format!("{id}-")))
        || !filename.ends_with(".jpg")
    {
        return Err("現像済みファイル名が不正です。".into());
    }
    // preview はディレクトリとレシピの両方で排除する。
    let core = data.join("coreimage");
    for (dir, expected) in [(core.clone(), data), (core.join("full"), core.as_path())] {
        if let Ok(m) = fs::symlink_metadata(&dir) {
            if !m.file_type().is_dir()
                || dir.canonicalize().map_err(io_error)?.parent() != Some(expected)
            {
                return Err("Core Image 出力フォルダが不正です。".into());
            }
        }
    }
    let full = core.join("full");
    let Some(image) = source_file(&full, filename)? else {
        if source_file(&core.join("preview"), filename)?.is_some() {
            return Err("プレビュー品質の画像は書き出せません。".into());
        }
        return Ok(None);
    };
    let recipe_name = Path::new(filename).with_extension("json");
    let recipe_path = source_file(&full, recipe_name.to_str().ok_or("レシピ名が不正です。")?)?
        .ok_or("現像レシピがありません。")?;
    let recipe: Value = serde_json::from_slice(&fs::read(recipe_path).map_err(io_error)?)
        .map_err(|e| e.to_string())?;
    if recipe["mode"] != "full"
        || recipe["request"]["mode"] != "full"
        || recipe["preview"].as_bool() == Some(true)
        || recipe["output"]["preview"].as_bool() == Some(true)
    {
        return Err("プレビュー品質またはモード不明の画像は書き出せません。".into());
    }
    if recipe["ok"] != true {
        return Err("Core Image の現像レシピが不正です。".into());
    }
    let input = recipe["request"]["input"]
        .as_str()
        .ok_or("レシピに現像元がありません。")?;
    let input = Path::new(input).canonicalize().map_err(io_error)?;
    if input.parent() != Some(root) || input.file_stem().and_then(|s| s.to_str()) != Some(id) {
        return Err("現像レシピの現像元フォルダが一致しません。".into());
    }
    if let Some(saved_root) = recipe.get("source_root").and_then(Value::as_str) {
        if Path::new(saved_root).canonicalize().map_err(io_error)? != root {
            return Err("現像元フォルダが一致しません。".into());
        }
    }
    if recipe["request"]["output"]
        .as_str()
        .map(Path::new)
        .and_then(|p| p.canonicalize().ok())
        .as_ref()
        != Some(&image)
    {
        return Err("レシピの出力先が一致しません。".into());
    }
    Ok(Some((image, recipe)))
}

fn relative(path: &Path, base: &Path) -> String {
    path.strip_prefix(base)
        .unwrap_or(path)
        .to_string_lossy()
        .into_owned()
}

fn export_batch(data: &Path, batch: &str, items: Vec<ExportItem>) -> Result<Value, String> {
    let _guard = EXPORT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let batch = normalize_batch_name(batch)?;
    if items.is_empty() || items.len() > 500 {
        return Err("書き出す写真は1〜500枚で指定してください。".into());
    }
    let data = data.canonicalize().map_err(io_error)?;
    let root = crate::coreimage::read_source_root(&data)
        .map_err(|e| e.message)?
        .canonicalize()
        .map_err(io_error)?;
    // manifest の相対パス基準は、このバッチのディレクトリ。外部の元画像は絶対パス。
    let manifest_base = data.join("export").join(&batch);
    let analysis = crate::analysis::read_cached_analysis(&data, &root).unwrap_or(Value::Null);
    let mut copies = Vec::new();
    let mut manifest_items = Vec::new();
    let mut skipped = Vec::new();
    let mut names = HashSet::new();
    for item in items {
        let id = item.capture_id.trim().to_uppercase();
        if !crate::coreimage::is_valid_capture_id(&id) {
            return Err("無効な撮影IDです。".into());
        }
        let jpg = source_file(&root, &format!("{id}.JPG"))?;
        let arw = match crate::coreimage::resolve_arw(&root, &id) {
            Ok(p) => Some(p),
            Err(e) if e.code == "ARW_NOT_FOUND" => None,
            Err(e) => return Err(e.message),
        };
        let (source, recipe, name) = match item.variant.as_str() {
            "camera_jpg" => (jpg.clone(), Value::Null, format!("{id}.JPG")),
            "developed" => {
                let name = item
                    .developed_filename
                    .ok_or("現像済みファイル名を指定してください。")?;
                let resolved = developed(&data, &root, &id, &name)?;
                match resolved {
                    Some((p, r)) => (Some(p), r, name),
                    None => (None, Value::Null, name),
                }
            }
            _ => return Err("variant は camera_jpg / developed を指定してください。".into()),
        };
        let Some(source) = source else {
            skipped.push(json!({"capture_id":id,"reason":if item.variant == "camera_jpg" {"元のJPGが見つかりません。"} else {"現像済みJPEGが見つかりません。"}}));
            continue;
        };
        if !names.insert(name.clone()) {
            skipped.push(json!({"capture_id":id,"reason":"同じファイルが重複しています。"}));
            continue;
        }
        let group = analysis["groups"].as_array().and_then(|groups| groups.iter().find(|g| g["capture_ids"].as_array().is_some_and(|ids| ids.contains(&json!(id))))).map(|g| json!({"id":g["id"],"kind":g["kind"],"raw_level":g["raw"]["level"],"was_recommended":g["recommended_capture_id"]==id,"user_selected":true}));
        let fallback = jpg.as_ref().and_then(|p| {
            let r = crate::analysis::read_exif(p, &id, 0, 0);
            if r.captured_at.is_none() && r.iso.is_none() && r.lens_model.is_none() && r.focal_length.is_none() { return None; }
            Some(json!({"captured_at":r.captured_at.map(|t|t.format("%Y-%m-%dT%H:%M:%S").to_string()),"iso":r.iso,"lens_model":r.lens_model,"focal_length":r.focal_length}))
        });
        let photo = analysis["photos"].get(&id).or(fallback.as_ref());
        let exif = photo.map(|p| json!({"captured_at":p["captured_at"],"iso":p["iso"],"lens_model":p["lens_model"],"focal_length":p["focal_length"]}));
        manifest_items.push(json!({"capture_id":id,"variant":item.variant,"exported_file":name,"exported_sha256":sha256(&source)?,"source":{"jpg":jpg.as_ref().map(|p|relative(p,&manifest_base)),"arw":arw.as_ref().map(|p|relative(p,&manifest_base)),"jpg_sha256":jpg.as_ref().map(|p|sha256(p)).transpose()?},"recipe":recipe,"group":group,"exif":exif}));
        copies.push((source, name));
    }
    let export_root = real_dir(&data, "export")?;
    let destination = real_dir(&export_root, &batch)?;
    let mut files = Vec::new();
    for (source, name) in copies {
        let target = destination.join(name);
        atomic_write(&target, |out| {
            let mut input = fs::File::open(source).map_err(io_error)?;
            let metadata = input.metadata().map_err(io_error)?;
            std::io::copy(&mut input, out).map_err(io_error)?;
            out.set_permissions(metadata.permissions())
                .map_err(io_error)?;
            let mut times = fs::FileTimes::new();
            if let Ok(t) = metadata.modified() {
                times = times.set_modified(t);
            }
            if let Ok(t) = metadata.accessed() {
                times = times.set_accessed(t);
            }
            out.set_times(times).map_err(io_error)?;
            Ok(())
        })?;
        files.push(target.to_string_lossy().into_owned());
    }
    let manifest = json!({"manifest_version":1,"app":"geoframe-raw-assist","batch_name":batch,"created_at":chrono::Utc::now().to_rfc3339(),"source_root":relative(&root,&manifest_base),"items":manifest_items});
    let manifest_path = destination.join("manifest.json");
    atomic_write(&manifest_path, |out| {
        out.write_all(format!("{}\n", serde_json::to_string_pretty(&manifest).unwrap()).as_bytes())
            .map_err(io_error)
    })?;
    Ok(
        json!({"export_dir":destination,"manifest_path":manifest_path,"files":files,"skipped":skipped}),
    )
}

#[tauri::command]
pub async fn export_selection(
    app: tauri::AppHandle,
    batch_name: String,
    items: Vec<ExportItem>,
) -> Result<Value, String> {
    let data = app.path().app_data_dir().map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || export_batch(&data, &batch_name, items))
        .await
        .map_err(|e| e.to_string())?
}

fn reveal_path(data: &Path, path: &str) -> Result<PathBuf, String> {
    let data = data.canonicalize().map_err(io_error)?;
    let root = data.join("export").canonicalize().map_err(io_error)?;
    let target = Path::new(path).canonicalize().map_err(io_error)?;
    if root.parent() != Some(data.as_path()) || !target.starts_with(&root) || target == root {
        return Err("書き出し先以外は表示できません。".into());
    }
    Ok(target)
}

#[tauri::command]
pub fn reveal_export(app: tauri::AppHandle, path: String) -> Result<(), String> {
    let target = reveal_path(
        &app.path().app_data_dir().map_err(|e| e.to_string())?,
        &path,
    )?;
    let status = std::process::Command::new("/usr/bin/open")
        .arg("-R")
        .arg(target)
        .status()
        .map_err(io_error)?;
    if status.success() {
        Ok(())
    } else {
        Err("Finder で表示できませんでした。".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    struct Fixture {
        data: PathBuf,
        root: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let data = std::env::temp_dir().join(format!(
                "export-test-{}-{}",
                std::process::id(),
                NONCE.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(data.join("raw")).unwrap();
            let data = data.canonicalize().unwrap();
            let root = data.join("raw");
            fs::write(
                data.join("config.json"),
                json!({"source_root":root}).to_string(),
            )
            .unwrap();
            fs::write(root.join("DSC00001.JPG"), b"original\0JPEG\xffEXIF GPS").unwrap();
            fs::write(root.join("DSC00001.ARW"), b"RAW").unwrap();
            fs::create_dir_all(data.join("coreimage/full")).unwrap();
            fs::create_dir_all(data.join("coreimage/preview")).unwrap();
            Self { data, root }
        }
        fn recipe(&self) -> Value {
            json!({"ok":true,"mode":"full","request":{"mode":"full","input":self.root.join("DSC00001.ARW"),"output":self.data.join("coreimage/full/DSC00001_hash.jpg")}})
        }
        fn save(&self, recipe: Value) {
            fs::write(
                self.data.join("coreimage/full/DSC00001_hash.jpg"),
                b"developed JPEG GPS",
            )
            .unwrap();
            fs::write(
                self.data.join("coreimage/full/DSC00001_hash.json"),
                recipe.to_string(),
            )
            .unwrap();
        }
        fn item(&self, variant: &str) -> ExportItem {
            ExportItem {
                capture_id: " dsc00001 ".into(),
                variant: variant.into(),
                developed_filename: Some("DSC00001_hash.jpg".into()),
            }
        }
        fn run(&self, variant: &str) -> Result<Value, String> {
            export_batch(&self.data, "batch", vec![self.item(variant)])
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.data);
        }
    }
    #[test]
    fn normalization_matches_python() {
        assert_eq!(normalize_batch_name(" 旅 2026! ").unwrap(), "__2026_");
        assert_eq!(normalize_batch_name(" Ab_09-x ").unwrap(), "Ab_09-x");
        for name in [
            "",
            "  ",
            "../x",
            "a/b",
            "a\\b",
            "a..b",
            "a\0b",
            &"a".repeat(65),
        ] {
            assert!(normalize_batch_name(name).is_err(), "{name:?}");
        }
        assert_eq!(
            normalize_batch_name(&"旅".repeat(64)).unwrap(),
            "_".repeat(64)
        );
    }
    #[test]
    fn manifest_baseline_shape_hash_and_byte_copy() {
        let f = Fixture::new();
        f.save(f.recipe());
        let response = export_batch(
            &f.data,
            "batch",
            vec![f.item("camera_jpg"), f.item("developed")],
        )
        .unwrap();
        let manifest: Value =
            serde_json::from_slice(&fs::read(response["manifest_path"].as_str().unwrap()).unwrap())
                .unwrap();
        let baseline: Value =
            serde_json::from_str(include_str!("../../tests/fixtures/baseline/manifest.json"))
                .unwrap();
        assert_eq!(
            manifest.as_object().unwrap().keys().collect::<HashSet<_>>(),
            baseline.as_object().unwrap().keys().collect::<HashSet<_>>()
        );
        for (i, original) in baseline["items"].as_array().unwrap().iter().enumerate() {
            for key in original.as_object().unwrap().keys() {
                assert!(manifest["items"][i].get(key).is_some(), "{key}");
            }
            assert_eq!(
                manifest["items"][i]["source"]
                    .as_object()
                    .unwrap()
                    .keys()
                    .collect::<HashSet<_>>(),
                original["source"]
                    .as_object()
                    .unwrap()
                    .keys()
                    .collect::<HashSet<_>>()
            );
        }
        for (i, source) in [
            f.root.join("DSC00001.JPG"),
            f.data.join("coreimage/full/DSC00001_hash.jpg"),
        ]
        .iter()
        .enumerate()
        {
            let copied = Path::new(response["files"][i].as_str().unwrap());
            assert_eq!(fs::read(copied).unwrap(), fs::read(source).unwrap());
            assert_eq!(
                manifest["items"][i]["exported_sha256"],
                sha256(copied).unwrap()
            );
            assert_eq!(
                manifest["items"][i]["source"]["jpg_sha256"],
                sha256(&f.root.join("DSC00001.JPG")).unwrap()
            );
        }
        assert_eq!(
            sha256(&f.root.join("DSC00001.ARW")).unwrap(),
            "ac0562bba4a57c6841cba10249e90ffc9895458763fae6885c9ee74fdce5ec90".to_string()
        );
        assert_eq!(manifest["items"][0]["exported_file"], "DSC00001.JPG");
        assert_eq!(manifest["source_root"], json!(f.root));
        assert_eq!(
            manifest["items"][0]["source"]["jpg"],
            json!(f.root.join("DSC00001.JPG"))
        );
        assert_eq!(
            relative(
                &f.data.join("export/batch/file.jpg"),
                &f.data.join("export/batch")
            ),
            "file.jpg"
        );
    }
    #[test]
    fn same_batch_overwrites_and_keeps_unmentioned_files() {
        let f = Fixture::new();
        f.run("camera_jpg").unwrap();
        let dir = f.data.join("export/batch");
        fs::write(dir.join("old.jpg"), b"keep").unwrap();
        fs::write(f.root.join("DSC00001.JPG"), b"new").unwrap();
        let response = export_batch(
            &f.data,
            "batch",
            vec![f.item("camera_jpg"), f.item("camera_jpg")],
        )
        .unwrap();
        assert_eq!(response["files"].as_array().unwrap().len(), 1);
        assert_eq!(response["skipped"].as_array().unwrap().len(), 1);
        assert_eq!(fs::read(dir.join("DSC00001.JPG")).unwrap(), b"new");
        assert!(dir.join("old.jpg").exists());
    }
    #[test]
    fn rejects_wrong_source_and_missing_recipe() {
        let f = Fixture::new();
        f.save(f.recipe());
        let mut r = f.recipe();
        r["source_root"] = json!(f.data);
        f.save(r);
        assert!(f.run("developed").is_err());
        let mut r = f.recipe();
        r["request"]["input"] = json!(f.data.join("DSC00001.ARW"));
        fs::write(f.data.join("DSC00001.ARW"), b"wrong").unwrap();
        f.save(r);
        assert!(f.run("developed").is_err());
        fs::remove_file(f.data.join("coreimage/full/DSC00001_hash.json")).unwrap();
        assert!(f.run("developed").is_err());
        assert!(!f.data.join("export").exists());
    }
    #[test]
    fn rejects_preview_in_directory_and_recipe() {
        let f = Fixture::new();
        fs::write(
            f.data.join("coreimage/preview/DSC00001_hash.jpg"),
            b"preview",
        )
        .unwrap();
        assert!(f.run("developed").is_err());
        let mut r = f.recipe();
        r["mode"] = json!("preview");
        f.save(r);
        assert!(f.run("developed").is_err());
        let mut r = f.recipe();
        r["output"] = json!({"preview":true});
        f.save(r);
        assert!(f.run("developed").is_err());
    }
    #[test]
    fn rejects_paths_and_symlinks() {
        let f = Fixture::new();
        f.save(f.recipe());
        for name in [
            "../DSC00001_hash.jpg",
            "/DSC00001_hash.jpg",
            "DSC00002_hash.jpg",
            "DSC00001_hash.json",
        ] {
            let mut item = f.item("developed");
            item.developed_filename = Some(name.into());
            assert!(export_batch(&f.data, "batch", vec![item]).is_err());
        }
        let image = f.data.join("coreimage/full/DSC00001_hash.jpg");
        fs::remove_file(&image).unwrap();
        symlink(f.root.join("DSC00001.JPG"), &image).unwrap();
        assert!(f.run("developed").is_err());
        fs::remove_file(f.root.join("DSC00001.JPG")).unwrap();
        symlink(f.data.join("DSC00001.ARW"), f.root.join("DSC00001.JPG")).unwrap();
        assert!(f.run("camera_jpg").is_err());
    }
    #[test]
    fn rejects_recipe_and_destination_directory_links() {
        let f = Fixture::new();
        f.save(f.recipe());
        let recipe = f.data.join("coreimage/full/DSC00001_hash.json");
        fs::rename(&recipe, f.data.join("outside.json")).unwrap();
        symlink(f.data.join("outside.json"), recipe).unwrap();
        assert!(f.run("developed").is_err());
        symlink(&f.root, f.data.join("export")).unwrap();
        assert!(f.run("camera_jpg").is_err());
    }
    #[test]
    fn missing_skipped_invalid_and_item_limits_rejected() {
        let f = Fixture::new();
        assert_eq!(
            f.run("developed").unwrap()["skipped"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert!(f.run("merged").is_err());
        assert!(export_batch(&f.data, "batch", vec![]).is_err());
        assert!(export_batch(
            &f.data,
            "batch",
            (0..501).map(|_| f.item("camera_jpg")).collect()
        )
        .is_err());
        let mut item = f.item("camera_jpg");
        item.capture_id = "../evil".into();
        assert!(export_batch(&f.data, "batch", vec![item]).is_err());
    }
    #[test]
    fn reveal_is_limited_to_export_tree() {
        let f = Fixture::new();
        let result = f.run("camera_jpg").unwrap();
        assert!(reveal_path(&f.data, result["export_dir"].as_str().unwrap()).is_ok());
        assert!(reveal_path(&f.data, f.root.to_str().unwrap()).is_err());
        symlink(&f.root, f.data.join("export/escape")).unwrap();
        assert!(reveal_path(&f.data, f.data.join("export/escape").to_str().unwrap()).is_err());
    }
}
