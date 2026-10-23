//! Core Image Exposure Fusion entry point (Issue #84).
//!
//! Rust owns capture validation, cache identity, output paths, EXIF inheritance, and provenance.
//! The sidecar receives one `mode: merge` request for all sorted RAW inputs.

use std::fs;
use std::path::Path;
use std::time::{Duration, UNIX_EPOCH};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};

use crate::adjustments::normalize_adjustments;
use crate::coreimage::{self, CoreImageError, DevelopSlot, EXIF_SOFTWARE, PREVIEW_MAX_EDGE};

const PREVIEW_DIR: &str = "merged_preview";
const FULL_DIR: &str = "merged";
const MERGE_SCHEMA: u32 = 1;
const MERGE_TIMEOUT_PREVIEW: Duration = Duration::from_secs(120);
const MERGE_TIMEOUT_FULL: Duration = Duration::from_secs(240);
const REASON_LENS_UNSUPPORTED: &str = "coreimage_unsupported";
const REASON_LENS_INCONSISTENT: &str = "inconsistent_across_frames";

/// Browser-facing result vocabulary shared with `MergedResult` in `src/lib/types.ts`.
#[derive(Debug, Clone, serde::Serialize)]
pub struct MergedResult {
    pub capture_ids: Vec<String>,
    pub frames: usize,
    pub preset: String,
    pub preset_label: String,
    pub filename: String,
    pub recipe_filename: String,
    pub params_hash: String,
    pub adjustments: Value,
    pub width: u64,
    pub height: u64,
    pub elapsed_seconds: f64,
    pub cached: bool,
    pub preview: bool,
    pub aligned: bool,
    pub lens_correction: Value,
    pub exif_inherited: bool,
    pub output_url: String,
}

fn invalid(code: &str, message: impl Into<String>) -> CoreImageError {
    CoreImageError::new(code, message)
}

fn validate_capture_ids(mut ids: Vec<String>) -> Result<Vec<String>, CoreImageError> {
    if !(2..=9).contains(&ids.len()) {
        return Err(invalid(
            "INVALID_CAPTURE_COUNT",
            "合成できるのは2〜9枚です。",
        ));
    }
    for id in &ids {
        if !coreimage::is_valid_capture_id(id) {
            return Err(invalid(
                "INVALID_CAPTURE_ID",
                format!("撮影IDの形式が不正です: {id:?}"),
            ));
        }
    }
    ids.sort();
    if ids.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(invalid(
            "DUPLICATE_CAPTURE_ID",
            "同じ撮影IDを複数回指定できません。",
        ));
    }
    Ok(ids)
}

fn hash_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn file_mtime_ns(metadata: &fs::Metadata) -> u128 {
    use std::os::unix::fs::MetadataExt;
    (metadata.mtime() as i128)
        .saturating_mul(1_000_000_000)
        .saturating_add(metadata.mtime_nsec() as i128)
        .max(0) as u128
}

fn file_fingerprint(path: &Path) -> Result<Value, CoreImageError> {
    let metadata = fs::metadata(path).map_err(|error| {
        invalid(
            "ARW_NOT_FOUND",
            format!("RAW ファイルを読めません: {error}"),
        )
    })?;
    Ok(json!({"size": metadata.len(), "mtime_ns": file_mtime_ns(&metadata).to_string()}))
}

fn pair_jpeg(root: &Path, lead: &str) -> Option<Vec<u8>> {
    let path = root.join(format!("{lead}.JPG"));
    let meta = fs::symlink_metadata(&path).ok()?;
    if !meta.file_type().is_file() || path.canonicalize().ok()?.parent() != Some(root) {
        return None;
    }
    fs::read(path).ok()
}

fn lens_summary(frames: &[Value]) -> Value {
    let states: Vec<bool> = frames
        .iter()
        .map(|frame| frame["applied"]["lens_correction_enabled"].as_bool() == Some(true))
        .collect();
    let first_lens = frames
        .first()
        .and_then(|frame| frame["source"]["lens_model"].as_str())
        .map(|lens| json!(lens));
    let mut summary = if states.iter().all(|state| *state) {
        json!({"applied": true, "flags": []})
    } else if states.iter().all(|state| !*state) {
        json!({"applied": false, "reason": REASON_LENS_UNSUPPORTED, "flags": []})
    } else {
        json!({"applied": false, "reason": REASON_LENS_INCONSISTENT, "flags": []})
    };
    if let Some(lens) = first_lens {
        summary["lens"] = lens.clone();
        summary["lens_model"] = lens;
    }
    summary
}

fn output_url(path: &Path) -> Result<String, CoreImageError> {
    let encoded: String = path
        .to_string_lossy()
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-_.!~*'()".contains(&byte) {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect();
    let modified = fs::metadata(path)
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    Ok(format!("asset://localhost/{encoded}?v={modified}"))
}

fn result_from_recipe(
    recipe: &Value,
    path: &Path,
    cached: bool,
) -> Result<MergedResult, CoreImageError> {
    let get_str = |value: &Value, key: &str| {
        value
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned()
    };
    let capture_ids: Vec<String> = recipe["capture_ids"]
        .as_array()
        .ok_or_else(|| invalid("SIDECAR_BAD_RESPONSE", "レシピの capture_ids が不正です"))?
        .iter()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect();
    recipe["frames"]
        .as_array()
        .ok_or_else(|| invalid("SIDECAR_BAD_RESPONSE", "レシピの frames が不正です"))?;
    let total = recipe["elapsed_ms"]["total"].as_f64().unwrap_or(0.0);
    Ok(MergedResult {
        frames: capture_ids.len(),
        capture_ids,
        preset: get_str(&recipe["preset"], "name"),
        preset_label: get_str(&recipe["preset"], "label"),
        filename: path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned(),
        recipe_filename: path
            .with_extension("json")
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned(),
        params_hash: get_str(recipe, "params_hash"),
        adjustments: recipe.get("adjustments").cloned().unwrap_or(Value::Null),
        width: recipe["width"]
            .as_u64()
            .ok_or_else(|| invalid("SIDECAR_BAD_RESPONSE", "合成幅がありません"))?,
        height: recipe["height"]
            .as_u64()
            .ok_or_else(|| invalid("SIDECAR_BAD_RESPONSE", "合成高さがありません"))?,
        elapsed_seconds: (total / 10.0).round() / 100.0,
        cached,
        preview: recipe["preview"].as_bool().unwrap_or(false),
        aligned: recipe["merge"]["aligned"].as_bool().unwrap_or(false),
        lens_correction: recipe
            .get("lens_correction")
            .cloned()
            .unwrap_or(json!({"applied":false,"reason":REASON_LENS_UNSUPPORTED})),
        exif_inherited: recipe["exif_inherited"].as_bool().unwrap_or(false),
        output_url: output_url(path)?,
    })
}

fn valid_cache(
    image: &Path,
    recipe_path: &Path,
    capture_ids: &[String],
    preset: &str,
    preset_label: &str,
    adjustments: &Value,
    preview: bool,
    source_root: &Path,
    request: &Value,
    params_hash: &str,
) -> Option<Value> {
    let image_meta = fs::symlink_metadata(image).ok()?;
    let recipe_meta = fs::symlink_metadata(recipe_path).ok()?;
    if !image_meta.file_type().is_file() || !recipe_meta.file_type().is_file() {
        return None;
    }
    let recipe: Value = serde_json::from_slice(&fs::read(recipe_path).ok()?).ok()?;
    let frames_match = recipe["frames"].as_array().is_some_and(|frames| {
        let Some(inputs) = request["inputs"].as_array() else {
            return false;
        };
        frames.len() == capture_ids.len()
            && frames.iter().zip(inputs).all(|(frame, input)| {
                &frame["input"] == input
                    && frame["width"].as_u64().is_some()
                    && frame["height"].as_u64().is_some()
                    && frame["applied"]["lens_correction_enabled"]
                        .as_bool()
                        .is_some()
                    && frame["source"].is_object()
            })
    });
    let complete = recipe["preset"]["label"] == preset_label
        && recipe["width"].as_u64().is_some_and(|width| width > 0)
        && recipe["height"].as_u64().is_some_and(|height| height > 0)
        && recipe["elapsed_ms"]["total"].as_f64().is_some()
        && recipe["merge"]["aligned"].as_bool().is_some()
        && recipe["lens_correction"] == lens_summary(recipe["frames"].as_array()?)
        && recipe["exif_inherited"].as_bool().is_some()
        && frames_match;
    (recipe["ok"] == true
        && recipe["kind"] == "exposure_fusion"
        && recipe["capture_ids"] == json!(capture_ids)
        && recipe["preset"]["name"] == preset
        && &recipe["adjustments"] == adjustments
        && recipe["preview"] == preview
        && recipe["source_root"] == json!(source_root.to_string_lossy())
        && &recipe["request"] == request
        && recipe["params_hash"] == params_hash
        && complete)
        .then_some(recipe)
}

fn remove_stale_cache_file(path: &Path) -> Result<(), CoreImageError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_file() || metadata.file_type().is_symlink() => {
            fs::remove_file(path).map_err(|error| {
                invalid(
                    "CACHE_IO_FAILED",
                    format!("合成キャッシュを置き換えられません: {error}"),
                )
            })
        }
        Ok(_) => Err(invalid(
            "OUTPUT_DIR_FAILED",
            "合成キャッシュの出力先が通常ファイルではありません",
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(invalid(
            "CACHE_IO_FAILED",
            format!("合成キャッシュを確認できません: {error}"),
        )),
    }
}

fn merge_blocking(
    app_data_dir: &Path,
    sidecar: &Path,
    capture_ids: Vec<String>,
    preset: String,
    adjustments: Value,
    preview: bool,
    force: bool,
    slot: &DevelopSlot,
    timeout: Duration,
) -> Result<MergedResult, CoreImageError> {
    let source_root = coreimage::read_source_root(app_data_dir)?
        .canonicalize()
        .map_err(|error| {
            invalid(
                "SOURCE_ROOT_INVALID",
                format!("現像元フォルダを解決できません: {error}"),
            )
        })?;
    let mut inputs = Vec::with_capacity(capture_ids.len());
    for id in &capture_ids {
        let input = coreimage::resolve_arw(&source_root, id)?;
        let canonical = input
            .canonicalize()
            .map_err(|error| invalid("ARW_NOT_FOUND", error.to_string()))?;
        if canonical.parent() != Some(source_root.as_path()) {
            return Err(invalid(
                "ARW_NOT_FOUND",
                "RAW は現像元フォルダ直下にありません",
            ));
        }
        inputs.push(canonical);
    }

    let version = coreimage::cli_version(sidecar)?;
    let preset_label = crate::presets::preset_infos()
        .into_iter()
        .find(|info| info.name == preset)
        .map(|info| info.label.to_string())
        .unwrap_or_default();
    let source_jpeg = if preview {
        None
    } else {
        pair_jpeg(&source_root, &capture_ids[0])
    };
    let jpeg_fingerprint = source_jpeg.as_ref().map(|bytes| hash_hex(bytes));
    let mut request = json!({
        "mode": "merge",
        "inputs": inputs.iter().map(|path| path.to_string_lossy().into_owned()).collect::<Vec<_>>(),
        "format": "jpeg",
        "jpeg_quality": 0.92,
        "decoder_version": "9",
        "settings": {"profile":"apple_default", "lens_correction":true},
        "adjustments": adjustments,
        "merge": {"align":true,"max_shift_ratio":0.02,"edge":"crop","weights":{"contrast":1,"saturation":1,"exposure":0}}
    });
    if preview {
        request["max_edge"] = json!(PREVIEW_MAX_EDGE);
    }
    let input_fingerprints: Vec<Value> = inputs
        .iter()
        .map(|path| file_fingerprint(path))
        .collect::<Result<_, _>>()?;
    let key_payload = json!({
        "schema": MERGE_SCHEMA,
        "request": request,
        "preset": preset,
        "source_root": source_root.to_string_lossy(),
        "inputs": input_fingerprints,
        "cli": version.fingerprint,
        "cli_binary_sha256": version.binary_sha256,
        "rust_exif": if preview { Value::Null } else { json!({"schema":1,"source_sha256":jpeg_fingerprint,"software":EXIF_SOFTWARE}) }
    });
    let digest = Sha256::digest(coreimage::canonical_json(&key_payload).as_bytes());
    let params_hash: String = digest
        .iter()
        .take(5)
        .map(|byte| format!("{byte:02x}"))
        .collect();

    let dir_name = if preview { PREVIEW_DIR } else { FULL_DIR };
    let (_, output_dir) = coreimage::prepare_output_subdir(app_data_dir, dir_name)?;
    coreimage::cleanup_stale_temps(&output_dir, coreimage::STALE_TEMP_AGE);
    let lead = &capture_ids[0];
    let stem = format!("{lead}-merged{}-{preset}-{params_hash}", capture_ids.len());
    let output = output_dir.join(format!("{stem}.jpg"));
    let provenance = output_dir.join(format!("{stem}.json"));
    if output.parent() != Some(output_dir.as_path())
        || provenance.parent() != Some(output_dir.as_path())
    {
        return Err(invalid(
            "OUTPUT_DIR_FAILED",
            "出力先が合成フォルダの外になります",
        ));
    }
    let mut expected_request = request.clone();
    expected_request["output"] = json!(output.to_string_lossy());
    if !force {
        if let Some(recipe) = valid_cache(
            &output,
            &provenance,
            &capture_ids,
            &preset,
            &preset_label,
            &key_payload["request"]["adjustments"],
            preview,
            &source_root,
            &expected_request,
            &params_hash,
        ) {
            return result_from_recipe(&recipe, &output, true);
        }
    }

    let _slot = slot.acquire()?;
    if !force {
        if let Some(recipe) = valid_cache(
            &output,
            &provenance,
            &capture_ids,
            &preset,
            &preset_label,
            &key_payload["request"]["adjustments"],
            preview,
            &source_root,
            &expected_request,
            &params_hash,
        ) {
            return result_from_recipe(&recipe, &output, true);
        }
    }
    remove_stale_cache_file(&output)?;
    remove_stale_cache_file(&provenance)?;
    request["output"] = json!(output.to_string_lossy());
    let body = request.to_string();
    let child = coreimage::run_sidecar(sidecar, &[], Some(body.as_bytes()), timeout)?;
    let mut response = coreimage::parse_response(&child)?;
    if response["ok"] != true || !child.status.success() {
        return Err(coreimage::cli_error(&response, child.status));
    }
    let output_canonical = output.canonicalize().ok();
    let response_output_canonical = response["output"]
        .as_str()
        .and_then(|path| Path::new(path).canonicalize().ok());
    if response_output_canonical.is_none()
        || response_output_canonical != output_canonical
        || !fs::symlink_metadata(&output).is_ok_and(|metadata| metadata.file_type().is_file())
        || output_canonical.as_ref().and_then(|path| path.parent()) != Some(output_dir.as_path())
    {
        return Err(invalid(
            "SIDECAR_BAD_RESPONSE",
            "Core Image CLI の出力先が依頼と一致しないか、出力ファイルがありません",
        ));
    }
    let response_inputs = response["inputs"].as_array();
    let inputs_match = response_inputs.is_some_and(|values| {
        values.len() == inputs.len()
            && values.iter().zip(&inputs).all(|(value, expected)| {
                value
                    .as_str()
                    .and_then(|path| Path::new(path).canonicalize().ok())
                    .as_ref()
                    == Some(expected)
            })
    });
    if response["mode"] != "merge" || response["format"] != "jpeg" || !inputs_match {
        return Err(invalid(
            "SIDECAR_BAD_RESPONSE",
            "Core Image CLI の合成応答が依頼と一致しません",
        ));
    }
    let width = response["width"]
        .as_u64()
        .ok_or_else(|| invalid("SIDECAR_BAD_RESPONSE", "合成幅がありません"))?;
    let height = response["height"]
        .as_u64()
        .ok_or_else(|| invalid("SIDECAR_BAD_RESPONSE", "合成高さがありません"))?;
    let frames = response["frames"]
        .as_array()
        .cloned()
        .ok_or_else(|| invalid("SIDECAR_BAD_RESPONSE", "合成フレーム情報がありません"))?;
    if frames.len() != capture_ids.len() {
        return Err(invalid(
            "SIDECAR_BAD_RESPONSE",
            "CLI のフレーム数が依頼と一致しません",
        ));
    }
    for (frame, input) in frames.iter().zip(&inputs) {
        if frame["input"]
            .as_str()
            .and_then(|path| Path::new(path).canonicalize().ok())
            .as_ref()
            != Some(input)
            || frame["width"].as_u64().is_none()
            || frame["height"].as_u64().is_none()
            || frame["applied"]["lens_correction_enabled"]
                .as_bool()
                .is_none()
            || !frame["source"].is_object()
        {
            return Err(invalid(
                "SIDECAR_BAD_RESPONSE",
                "CLI のフレーム metadata が依頼と一致しないか、不完全です",
            ));
        }
    }
    response["inputs"] = json!(inputs
        .iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect::<Vec<_>>());
    response["output"] = json!(output_canonical.unwrap().to_string_lossy().into_owned());
    if response["merge"]["merge_version"].as_u64().is_none()
        || response["elapsed_ms"]["total"].as_f64().is_none()
    {
        return Err(invalid(
            "SIDECAR_BAD_RESPONSE",
            "CLI の合成 metadata または処理時間が不完全です",
        ));
    }
    let frames = response["frames"]
        .as_array_mut()
        .expect("validated frame array");
    for (frame, input) in frames.iter_mut().zip(&inputs) {
        frame["input"] = json!(input.to_string_lossy().into_owned());
    }
    let exif_inherited = if preview {
        false
    } else {
        source_jpeg
            .as_deref()
            .is_some_and(|source| coreimage::inherit_output_exif(&output, source, width, height))
    };
    let lens = lens_summary(&frames);
    response["kind"] = json!("exposure_fusion");
    response["capture_ids"] = json!(capture_ids);
    response["preset"] = json!({"name": preset, "label": preset_label});
    response["adjustments"] = key_payload["request"]["adjustments"].clone();
    response["preview"] = json!(preview);
    response["source_root"] = json!(source_root.to_string_lossy());
    response["params_hash"] = json!(params_hash);
    response["exif_inherited"] = json!(exif_inherited);
    response["engine"] = json!({"name":"coreimage mtb+mertens","cli_version":version.cli_version,"macos_version":version.macos_version,"merge_version":response["merge"]["merge_version"]});
    response["request"] = serde_json::from_str(&body).expect("serialized request");
    response["lens_correction"] = lens;
    coreimage::write_provenance(&provenance, &format!("{}\n", response))?;
    coreimage::prune_cache(
        &output_dir,
        coreimage::CACHE_MAX_ENTRIES,
        coreimage::CACHE_MAX_BYTES,
        Some(&stem),
    );
    result_from_recipe(&response, &output, false)
}

/// 合成 2〜9 枚のブラケットを単一の Core Image CLI リクエストで現像する。
#[tauri::command]
pub async fn merge_bracket(
    app: AppHandle,
    capture_ids: Vec<String>,
    preset: String,
    adjustments: Value,
    preview: bool,
    force: bool,
) -> Result<Value, CoreImageError> {
    let ids = validate_capture_ids(capture_ids)?;
    let preset_info = crate::presets::preset_infos()
        .into_iter()
        .find(|info| info.name == preset)
        .ok_or_else(|| {
            invalid(
                crate::presets::UNKNOWN_PRESET,
                format!("未対応のプリセットです: {preset:?}"),
            )
        })?;
    let adjustments = crate::presets::resolve_adjustments(&preset, Some(&adjustments))?;
    let adjustments = normalize_adjustments(&adjustments)?;
    let app_data_dir = app.path().app_data_dir().map_err(|error| {
        invalid(
            "APP_DATA_DIR_UNAVAILABLE",
            format!("アプリのデータフォルダを取得できません: {error}"),
        )
    })?;
    let sidecar = coreimage::sidecar_path()?;
    let timeout = if preview {
        MERGE_TIMEOUT_PREVIEW
    } else {
        MERGE_TIMEOUT_FULL
    };
    tauri::async_runtime::spawn_blocking(move || {
        let mut result = merge_blocking(
            &app_data_dir,
            &sidecar,
            ids,
            preset,
            adjustments,
            preview,
            force,
            &coreimage::DEVELOP_SLOT,
            timeout,
        )?;
        // Label is derived from the registered preset table; keep this aligned with `get_presets`.
        result.preset_label = preset_info.label.to_string();
        serde_json::to_value(result).map_err(|error| invalid("INTERNAL_ERROR", error.to_string()))
    })
    .await
    .map_err(|error| {
        invalid(
            "INTERNAL_ERROR",
            format!("合成スレッドが異常終了しました: {error}"),
        )
    })?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::os::unix::fs::PermissionsExt;

    struct TestDir(PathBuf);

    impl TestDir {
        fn new(name: &str) -> Self {
            static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "merge-bracket-{name}-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }
    impl Drop for TestDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn mock_cli(dir: &Path) -> PathBuf {
        let path = dir.join("mock-merge-cli");
        let calls = dir.join("calls");
        let script = format!(
            r##"#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ["--version"]:
    print(json.dumps({{"ok":True,"engine":"coreimage","cli_version":"0.7.0","macos_version":"27.0.1"}}))
    raise SystemExit(0)
request=json.load(sys.stdin)
with open({calls:?}, "a") as out: out.write("merge\n")
with open(request["output"], "wb") as out: out.write(b"synthetic merged jpeg")
frames=[{{"input":p,"width":20,"height":10,"applied":{{"lens_correction_enabled":True}},"source":{{"lens_model":"Fixture Lens"}}}} for p in request["inputs"]]
response={{"ok":True,"engine":"coreimage","cli_version":"0.7.0","macos_version":"27.0.1","decoder_version":"9","mode":"merge","format":"jpeg","inputs":request["inputs"],"output":request["output"],"width":20,"height":10,"elapsed_ms":{{"decode":2.0,"align":1.0,"fuse":3.0,"post_encode":1.0,"total":7.0}},"merge":{{"algorithm":"mtb+mertens","merge_version":1,"aligned":True,"shifts_xy":[[0,0],[1,0]],"applied_shifts_xy":[[0,0],[1,0]],"crop":{{"x":1,"y":0,"width":19,"height":10}},"weights":{{"contrast":1,"saturation":1,"exposure":0}},"edge":"crop"}},"frames":frames,"adjustments_applied":{{"ignored":["exposure","highlight_recovery"]}}}}
print(json.dumps(response))
"##,
            calls = calls.display()
        );
        fs::write(&path, script).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn mock_cli_with_response_edit(dir: &Path, edit: &str) -> PathBuf {
        let path = mock_cli(dir);
        let script = fs::read_to_string(&path).unwrap().replace(
            "print(json.dumps(response))",
            &format!("{edit}\nprint(json.dumps(response))"),
        );
        fs::write(&path, script).unwrap();
        path
    }

    fn fixture() -> (TestDir, TestDir, TestDir, PathBuf, Value) {
        let app = TestDir::new("app");
        let source = TestDir::new("source");
        for id in ["DSC00002", "DSC00001"] {
            fs::write(source.path().join(format!("{id}.ARW")), id.as_bytes()).unwrap();
        }
        fs::write(
            app.path().join("config.json"),
            json!({"source_root":source.path()}).to_string(),
        )
        .unwrap();
        let bin = TestDir::new("bin");
        let cli = mock_cli(bin.path());
        let adjustments = crate::presets::resolve_adjustments("neutral_recovery", None).unwrap();
        (app, source, bin, cli, adjustments)
    }

    fn error_cli(dir: &Path) -> PathBuf {
        let path = dir.join("error-cli");
        let script = r#"#!/bin/sh
if [ "$1" = "--version" ]; then
  printf '%s\n' '{"ok":true,"engine":"coreimage","cli_version":"0.7.0","macos_version":"27.0.1"}'
  exit 0
fi
cat >/dev/null
printf '%s\n' '{"ok":false,"error":{"code":"INVALID_REQUEST","message":"mock rejected"}}'
exit 1
"#;
        fs::write(&path, script).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    #[test]
    fn lens_reason_vocabulary_is_stable() {
        let ui_vocabulary: [&str; 2] = serde_json::from_str(include_str!(
            "../../tests/fixtures/coreimage_merge_lens_reasons.json"
        ))
        .expect("shared UI lens reason fixture must be valid JSON");
        assert_eq!(
            ui_vocabulary,
            [REASON_LENS_UNSUPPORTED, REASON_LENS_INCONSISTENT]
        );
        assert_eq!(REASON_LENS_UNSUPPORTED, "coreimage_unsupported");
        assert_eq!(REASON_LENS_INCONSISTENT, "inconsistent_across_frames");
        let supported = lens_summary(&[
            json!({"applied":{"lens_correction_enabled":true}}),
            json!({"applied":{"lens_correction_enabled":true}}),
        ]);
        assert_eq!(supported["applied"], true);
        let mixed = lens_summary(&[
            json!({"applied":{"lens_correction_enabled":true}}),
            json!({"applied":{"lens_correction_enabled":false}}),
        ]);
        assert_eq!(mixed["reason"], "inconsistent_across_frames");
        let unsupported = lens_summary(&[
            json!({"applied":{"lens_correction_enabled":false}}),
            json!({"applied":{"lens_correction_enabled":false}}),
        ]);
        assert_eq!(unsupported["reason"], "coreimage_unsupported");
    }

    #[test]
    fn accepts_two_to_nine_and_rejects_one_or_ten() {
        for count in [2, 9] {
            let ids = (0..count).map(|n| format!("DSC{n:05}")).collect();
            assert_eq!(validate_capture_ids(ids).unwrap().len(), count);
        }
        for count in [1, 10] {
            let ids = (0..count).map(|n| format!("DSC{n:05}")).collect();
            assert_eq!(
                validate_capture_ids(ids).unwrap_err().code,
                "INVALID_CAPTURE_COUNT"
            );
        }
        assert_eq!(
            validate_capture_ids(vec!["DSC00002".into(), "DSC00001".into()]).unwrap(),
            ["DSC00001", "DSC00002"]
        );
        assert_eq!(
            validate_capture_ids(vec!["DSC00001".into(), "DSC00001".into()])
                .unwrap_err()
                .code,
            "DUPLICATE_CAPTURE_ID"
        );
    }

    #[test]
    fn forwards_structured_cli_error_and_uses_merge_watchdogs() {
        let (app, _source, bin, _cli, adjustments) = fixture();
        let cli = error_cli(bin.path());
        let error = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            adjustments,
            true,
            false,
            &DevelopSlot::new(2),
            Duration::from_secs(10),
        )
        .unwrap_err();
        assert_eq!(error.code, "INVALID_REQUEST");
        assert_eq!(MERGE_TIMEOUT_PREVIEW.as_secs(), 120);
        assert_eq!(MERGE_TIMEOUT_FULL.as_secs(), 240);
    }

    #[test]
    fn mock_cli_request_recipe_cache_force_and_output_split() {
        let (app, source, _bin, cli, adjustments) = fixture();
        let slot = DevelopSlot::new(2);
        let first = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            adjustments.clone(),
            true,
            false,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert!(!first.cached);
        assert_eq!(first.capture_ids, ["DSC00001", "DSC00002"]);
        assert_eq!(
            first.filename,
            format!(
                "DSC00001-merged2-neutral_recovery-{}.jpg",
                first.params_hash
            )
        );
        assert!(first.filename.contains("-merged2-"));
        assert!(Path::new(&first.output_url).is_absolute() == false);
        assert!(first.output_url.contains("merged_preview"));
        let recipe_path = app
            .path()
            .join("coreimage/merged_preview")
            .join(first.recipe_filename.clone());
        let recipe: Value = serde_json::from_slice(&fs::read(recipe_path).unwrap()).unwrap();
        assert_eq!(recipe["kind"], "exposure_fusion");
        assert_eq!(recipe["request"]["mode"], "merge");
        assert_eq!(recipe["request"]["max_edge"], json!(2400));
        assert_eq!(recipe["request"]["format"], "jpeg");
        assert_eq!(recipe["request"]["jpeg_quality"], json!(0.92));
        assert_eq!(recipe["request"]["decoder_version"], "9");
        assert_eq!(
            recipe["request"]["settings"],
            json!({"profile":"apple_default","lens_correction":true})
        );
        assert_eq!(
            recipe["request"]["inputs"][0],
            json!(source
                .path()
                .join("DSC00001.ARW")
                .canonicalize()
                .unwrap()
                .to_string_lossy())
        );
        assert_eq!(recipe["request"]["merge"]["weights"]["exposure"], json!(0));
        assert_eq!(recipe["request"]["merge"]["align"], true);
        assert_eq!(recipe["request"]["merge"]["max_shift_ratio"], json!(0.02));
        assert_eq!(recipe["request"]["merge"]["edge"], "crop");
        assert_eq!(recipe["adjustments"], adjustments);
        assert_eq!(
            recipe["adjustments_applied"]["ignored"],
            json!(["exposure", "highlight_recovery"])
        );
        assert_eq!(
            fs::read_to_string(cli.parent().unwrap().join("calls"))
                .unwrap()
                .lines()
                .count(),
            1
        );

        let cached = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            adjustments.clone(),
            true,
            false,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert!(cached.cached);
        let forced = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            adjustments.clone(),
            true,
            true,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert!(!forced.cached);
        assert_eq!(
            fs::read_to_string(cli.parent().unwrap().join("calls"))
                .unwrap()
                .lines()
                .count(),
            2
        );

        let full = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            adjustments,
            false,
            false,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert!(!full.preview);
        assert!(full.output_url.contains("coreimage%2Fmerged%2F"));
        assert!(app
            .path()
            .join("coreimage/merged")
            .join(full.filename)
            .is_file());

        fs::write(
            source.path().join("DSC00001.JPG"),
            b"synthetic pair exif one",
        )
        .unwrap();
        let with_pair = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            crate::presets::resolve_adjustments("neutral_recovery", None).unwrap(),
            false,
            false,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert_ne!(with_pair.params_hash, full.params_hash);
        fs::write(
            source.path().join("DSC00001.JPG"),
            b"synthetic pair exif two",
        )
        .unwrap();
        let pair_changed = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            crate::presets::resolve_adjustments("neutral_recovery", None).unwrap(),
            false,
            false,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert_ne!(pair_changed.params_hash, with_pair.params_hash);
        fs::write(source.path().join("DSC00002.ARW"), b"changed raw size").unwrap();
        let raw_changed = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            crate::presets::resolve_adjustments("neutral_recovery", None).unwrap(),
            false,
            false,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert_ne!(raw_changed.params_hash, pair_changed.params_hash);
        assert_eq!(
            fs::read_to_string(cli.parent().unwrap().join("calls"))
                .unwrap()
                .lines()
                .count(),
            6
        );

        let upgraded_bin = TestDir::new("upgraded-bin");
        let upgraded_cli = mock_cli(upgraded_bin.path());
        let mut upgraded = fs::read_to_string(&upgraded_cli).unwrap();
        upgraded.push_str("# changed CLI binary fixture\n");
        fs::write(&upgraded_cli, upgraded).unwrap();
        fs::set_permissions(&upgraded_cli, fs::Permissions::from_mode(0o755)).unwrap();
        let upgraded_result = merge_blocking(
            app.path(),
            &upgraded_cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            crate::presets::resolve_adjustments("neutral_recovery", None).unwrap(),
            false,
            false,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert_ne!(upgraded_result.params_hash, raw_changed.params_hash);

        let alternate_source = TestDir::new("alternate-source");
        for id in ["DSC00001", "DSC00002"] {
            fs::write(
                alternate_source.path().join(format!("{id}.ARW")),
                b"alternate raw",
            )
            .unwrap();
        }
        fs::write(
            app.path().join("config.json"),
            json!({"source_root":alternate_source.path()}).to_string(),
        )
        .unwrap();
        let switched = merge_blocking(
            app.path(),
            &upgraded_cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            crate::presets::resolve_adjustments("neutral_recovery", None).unwrap(),
            false,
            false,
            &slot,
            Duration::from_secs(10),
        )
        .unwrap();
        assert_ne!(switched.params_hash, upgraded_result.params_hash);
    }

    #[test]
    fn mock_cli_accepts_canonical_aliases_and_rejects_mismatches() {
        let (app, source, bin, _cli, adjustments) = fixture();
        let alias_dir = source.path().join("alias");
        std::os::unix::fs::symlink(source.path(), &alias_dir).unwrap();
        let cli = mock_cli_with_response_edit(
            bin.path(),
            r#"response["inputs"]=[p.replace(os.path.dirname(p)+"/", os.path.dirname(p)+"/alias/") for p in response["inputs"]]
for frame in response["frames"]: frame["input"]=frame["input"].replace(os.path.dirname(frame["input"])+"/", os.path.dirname(frame["input"])+"/alias/")
response["output"] += ".alias"
os.symlink(request["output"], response["output"])"#,
        );
        let accepted = merge_blocking(
            app.path(),
            &cli,
            vec!["DSC00001".into(), "DSC00002".into()],
            "neutral_recovery".into(),
            adjustments.clone(),
            true,
            false,
            &DevelopSlot::new(2),
            Duration::from_secs(10),
        )
        .unwrap();
        let accepted_recipe: Value = serde_json::from_slice(
            &fs::read(
                app.path()
                    .join("coreimage")
                    .join(PREVIEW_DIR)
                    .join(&accepted.recipe_filename),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            accepted_recipe["inputs"],
            json!([
                source
                    .path()
                    .join("DSC00001.ARW")
                    .canonicalize()
                    .unwrap()
                    .to_string_lossy(),
                source
                    .path()
                    .join("DSC00002.ARW")
                    .canonicalize()
                    .unwrap()
                    .to_string_lossy()
            ])
        );
        assert_eq!(
            accepted_recipe["output"],
            json!(app
                .path()
                .join("coreimage")
                .join(PREVIEW_DIR)
                .join(&accepted.filename)
                .canonicalize()
                .unwrap()
                .to_string_lossy())
        );
        assert!(accepted_recipe["frames"]
            .as_array()
            .unwrap()
            .iter()
            .all(|frame| {
                frame["input"].as_str().is_some_and(|input| {
                    Path::new(input).parent() == source.path().canonicalize().ok().as_deref()
                })
            }));

        for (case, edit) in [
            (
                "different root",
                r#"response["inputs"][1]="/different/root/DSC00002.ARW""#,
            ),
            (
                "different file",
                r#"response["inputs"][0]=response["inputs"][1]"#,
            ),
            ("wrong order", r#"response["inputs"].reverse()"#),
            ("non-string input", r#"response["inputs"][1]=7"#),
            (
                "frame input mismatch",
                r#"response["frames"][1]["input"]="/different/root/DSC00002.ARW""#,
            ),
            (
                "output mismatch",
                r#"response["output"]="/different/root/output.jpg""#,
            ),
        ] {
            let (app, _source, bin, _cli, adjustments) = fixture();
            let cli = mock_cli_with_response_edit(bin.path(), edit);
            let error = merge_blocking(
                app.path(),
                &cli,
                vec!["DSC00001".into(), "DSC00002".into()],
                "neutral_recovery".into(),
                adjustments,
                true,
                false,
                &DevelopSlot::new(2),
                Duration::from_secs(10),
            )
            .unwrap_err();
            assert_eq!(
                error.code, "SIDECAR_BAD_RESPONSE",
                "{case} must be rejected"
            );
        }
    }

    #[test]
    fn merge_cache_requires_matching_complete_recipe() {
        let (app, _source, _bin, cli, adjustments) = fixture();
        let slot = DevelopSlot::new(1);
        let ids = vec!["DSC00001".to_owned(), "DSC00002".to_owned()];
        let merge = || {
            merge_blocking(
                app.path(),
                &cli,
                ids.clone(),
                "neutral_recovery".into(),
                adjustments.clone(),
                true,
                false,
                &slot,
                Duration::from_secs(10),
            )
            .unwrap()
        };
        let calls_path = cli.parent().unwrap().join("calls");
        let first = merge();
        assert!(!first.cached);
        let recipe_path = app
            .path()
            .join("coreimage/merged_preview")
            .join(&first.recipe_filename);
        let output_path = app
            .path()
            .join("coreimage/merged_preview")
            .join(&first.filename);
        assert!(merge().cached, "matching cache entry should be reused");
        assert_eq!(fs::read_to_string(&calls_path).unwrap().lines().count(), 1);

        for corruption in [
            "hash",
            "request",
            "preview",
            "capture_ids",
            "preset",
            "adjustments",
            "source_root",
            "lens_summary",
            "incomplete_json",
        ] {
            match corruption {
                "incomplete_json" => fs::write(&recipe_path, b"{}").unwrap(),
                _ => {
                    let mut recipe: Value =
                        serde_json::from_slice(&fs::read(&recipe_path).unwrap()).unwrap();
                    match corruption {
                        "hash" => recipe["params_hash"] = json!("wrong-hash"),
                        "request" => recipe["request"]["merge"]["align"] = json!(false),
                        "preview" => recipe["preview"] = json!(false),
                        "capture_ids" => recipe["capture_ids"] = json!(["DSC00003", "DSC00004"]),
                        "preset" => recipe["preset"]["name"] = json!("landscape_clear"),
                        "adjustments" => recipe["adjustments"]["corrupt"] = json!(true),
                        "source_root" => recipe["source_root"] = json!("/wrong/source"),
                        "lens_summary" => {
                            for frame in recipe["frames"].as_array_mut().unwrap() {
                                frame["applied"]["lens_correction_enabled"] = json!(false);
                            }
                        }
                        _ => unreachable!(),
                    }
                    fs::write(&recipe_path, serde_json::to_vec(&recipe).unwrap()).unwrap();
                }
            }

            let regenerated = merge();
            assert!(
                !regenerated.cached,
                "{corruption} cache must be regenerated"
            );
            assert_eq!(regenerated.capture_ids, ids);
            assert!(output_path.is_file());
            assert_eq!(
                fs::read_to_string(&calls_path).unwrap().lines().count(),
                2 + [
                    "hash",
                    "request",
                    "preview",
                    "capture_ids",
                    "preset",
                    "adjustments",
                    "source_root",
                    "lens_summary",
                    "incomplete_json",
                ]
                .iter()
                .position(|candidate| candidate == &corruption)
                .unwrap()
            );
        }
    }

    #[test]
    #[ignore = "requires GEOFRAME_MERGE_SMOKE_CLI and GEOFRAME_MERGE_SMOKE_ARW"]
    fn real_cli_merge_export_smoke() {
        use crate::export::{export_batch, ExportItem};

        let required_path = |name: &str| -> Option<PathBuf> {
            let Some(raw) = std::env::var_os(name) else {
                eprintln!("SKIP: {name} is required to run the real Core Image merge smoke test");
                return None;
            };
            let path = PathBuf::from(raw);
            if !path.is_absolute() || !path.is_file() {
                eprintln!("SKIP: {name} must be an absolute path to an existing file");
                return None;
            }
            Some(path)
        };
        let Some(cli) = required_path("GEOFRAME_MERGE_SMOKE_CLI") else {
            return;
        };
        let Some(arw) = required_path("GEOFRAME_MERGE_SMOKE_ARW") else {
            return;
        };

        let app = TestDir::new("real-cli-app");
        let source = TestDir::new("real-cli-source");
        for id in ["DSC00001", "DSC00002"] {
            fs::copy(&arw, source.path().join(format!("{id}.ARW")))
                .expect("copy source RAW into isolated smoke directory");
        }
        let original = fs::read(&arw).expect("read-only source RAW");
        assert_eq!(
            fs::read(source.path().join("DSC00001.ARW")).unwrap(),
            original
        );
        assert_eq!(
            fs::read(source.path().join("DSC00002.ARW")).unwrap(),
            original
        );
        fs::write(
            app.path().join("config.json"),
            json!({"source_root":source.path()}).to_string(),
        )
        .unwrap();

        // The paired JPEG is synthetic and contains no real location metadata.
        let pair_jpeg = crate::exif_inherit::tests::synthetic_gps_jpeg();
        fs::write(source.path().join("DSC00001.JPG"), &pair_jpeg).unwrap();
        let slot = DevelopSlot::new(1);
        let adjustments = crate::presets::resolve_adjustments("neutral_recovery", None).unwrap();
        let ids = vec!["DSC00001".to_owned(), "DSC00002".to_owned()];
        let preview = merge_blocking(
            app.path(),
            &cli,
            ids.clone(),
            "neutral_recovery".into(),
            adjustments.clone(),
            true,
            false,
            &slot,
            MERGE_TIMEOUT_PREVIEW,
        )
        .expect("real CLI preview merge");
        assert!(preview.preview);
        assert!(preview.width > 0);
        let preview_recipe: Value = serde_json::from_slice(
            &fs::read(
                app.path()
                    .join("coreimage")
                    .join(PREVIEW_DIR)
                    .join(&preview.recipe_filename),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(preview_recipe["request"]["max_edge"], json!(2400));

        let full = merge_blocking(
            app.path(),
            &cli,
            ids.clone(),
            "neutral_recovery".into(),
            adjustments.clone(),
            false,
            false,
            &slot,
            MERGE_TIMEOUT_FULL,
        )
        .expect("real CLI full merge");
        assert!(!full.preview);
        let full_recipe: Value = serde_json::from_slice(
            &fs::read(
                app.path()
                    .join("coreimage")
                    .join(FULL_DIR)
                    .join(&full.recipe_filename),
            )
            .unwrap(),
        )
        .unwrap();
        assert!(full_recipe["request"].get("max_edge").is_none());
        let full_path = app
            .path()
            .join("coreimage")
            .join(FULL_DIR)
            .join(&full.filename);
        let full_bytes = fs::read(&full_path).unwrap();
        crate::exif_inherit::tests::assert_synthetic_gps_metadata(
            &full_bytes,
            full.width,
            full.height,
        );

        let cached = merge_blocking(
            app.path(),
            &cli,
            ids,
            "neutral_recovery".into(),
            adjustments,
            false,
            false,
            &slot,
            MERGE_TIMEOUT_FULL,
        )
        .expect("full cache hit");
        assert!(cached.cached);
        assert_eq!(cached.filename, full.filename);
        assert_eq!(fs::read(&full_path).unwrap(), full_bytes);

        let item = |variant: &str, filename: &str| ExportItem {
            capture_id: "DSC00001".into(),
            variant: variant.into(),
            developed_filename: None,
            merged_filename: Some(filename.into()),
        };
        let exported = export_batch(
            app.path(),
            "real-cli-smoke",
            vec![item("merged", &full.filename)],
        )
        .expect("export verified full merged result");
        let export_path = PathBuf::from(exported["files"][0].as_str().unwrap());
        assert_eq!(fs::read(export_path).unwrap(), full_bytes);
        let manifest: Value =
            serde_json::from_slice(&fs::read(exported["manifest_path"].as_str().unwrap()).unwrap())
                .unwrap();
        assert_eq!(manifest["items"][0]["variant"], "merged");
        assert_eq!(manifest["items"][0]["recipe"], full_recipe);
        assert_eq!(
            manifest["items"][0]["exported_sha256"],
            json!(hash_hex(&full_bytes))
        );
        assert!(export_batch(
            app.path(),
            "real-cli-preview-rejected",
            vec![item("merged", &preview.filename)],
        )
        .is_err());
        assert_eq!(
            fs::read(&arw).expect("source RAW remains readable"),
            original,
            "real CLI smoke must leave the source RAW unchanged"
        );
    }
}
