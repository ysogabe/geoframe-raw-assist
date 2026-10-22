//! 解析 lite（Issue #110 PR(1)。設計書 `docs/design/single-app-implementation-plan.md` §3.3）。
//!
//! Python `local_raw/analysis.py` / `source_files.py` から **採点を除いて** 移植する。範囲は
//! 走査・EXIF 要約・長辺 640 の JPEG サムネイル・グループ化と kind 判定・RAW 推薦（EXIF 由来の分だけ）。
//!
//! - 出力 JSON はフロントの `Analysis` 型（`src/lib/types.ts`）とキー名を合わせる。ただし
//!   `analysis_version` は **3**、`scored: false` を足す（採点は N-C2。Python の v2 キャッシュは読まない）。
//! - 採点由来の値の決め方（UI が嘘を出さないための取り決め）: `sharpness_raw` / `sharpness_score` /
//!   `highlight_clip_ratio` / `shadow_clip_ratio` / `mean_luma` / `overall_score` は 0。
//!   `recommended_capture_id` は時刻順の先頭、`confidence` は 0.5、`recommendation_reasons` は空。
//!   `raw.reason_codes` は EXIF 由来の `high_iso` / `exposure_bracket` だけ。`summary.needs_review` は 0。
//! - GPS・撮影地の情報は **読まない**（EXIF のタグを名指しで読むだけで、GPS IFD には触れない）。
//! - キャッシュ: `$APPDATA/analysis/{root_key}.json`。サムネイル: `$APPDATA/thumbnails/{root_key}-{capture_id}.jpg`。
//!   `photos[*].thumb` は **サムネイルの絶対パス**（フロントは `convertFileSrc` に渡す。asset scope は
//!   `tauri.conf.json` の `$APPDATA/thumbnails/**`）。
//! - 元 JPG の配信は自前の URI scheme `geoframe`（`geoframe://localhost/source-jpg/DSC12345.JPG`）。
//!   asset protocol の scope は実行時に「許可の取り消し」ができないため案 2 にした（`source_jpg_protocol`）。

use std::collections::BTreeMap;
use std::fs;
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use chrono::NaiveDateTime;
use regex::Regex;
use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};
use tauri::http::{self, Request, Response};
use tauri::{AppHandle, Manager, Runtime, UriSchemeContext, UriSchemeResponder};

use crate::coreimage::{is_valid_capture_id, read_source_root, root_key_for, CoreImageError};

pub const ANALYSIS_VERSION: u32 = 3;

// --- pipeline tuning（Python と同値） -------------------------------------------
const MAX_WORKERS: usize = 6;
const THUMB_MAX_EDGE: u32 = 640;
const THUMB_QUALITY: u8 = 80;

// --- grouping --------------------------------------------------------------------
const GROUP_GAP_SECONDS: f64 = 60.0;
const GROUP_FOCAL_RATIO: f64 = 1.5;
const BURST_MEDIAN_GAP_SECONDS: f64 = 1.5;
const BRACKET_MIN_DISTINCT_BIAS: usize = 2;
const BRACKET_MIN_SPAN_EV: f64 = 0.6;
const BRACKET_SPAN_EPSILON: f64 = 1e-6;

// --- RAW recommendation（EXIF 由来の分だけ） ----------------------------------------
const RAW_RECOMMENDED_ISO: i64 = 800;
const RAW_MAYBE_EXPOSURE_BIAS: f64 = -0.7;

/// lite の `confidence`（Python の `CONFIDENCE_FLOOR`）。
const LITE_CONFIDENCE: f64 = 0.5;

// ---------------------------------------------------------------------------
// 走査（local_raw/source_files.py）
// ---------------------------------------------------------------------------

fn entry_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    // ステムは大文字固定、拡張子だけ大文字小文字を問わない（source_files._ENTRY_PATTERN）。
    PATTERN.get_or_init(|| Regex::new(r"^(DSC[0-9]{5})\.((?i:ARW|JPG))$").expect("valid regex"))
}

/// `(実エントリ名, capture_id, 種別 "ARW"|"JPG")` を実名の辞書順で返す。
fn entries(root: &Path) -> Vec<(String, String, &'static str)> {
    let Ok(read) = fs::read_dir(root) else {
        return Vec::new();
    };
    let mut names: Vec<String> = read
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| entry.file_name().into_string().ok())
        .collect();
    names.sort();
    names
        .into_iter()
        .filter_map(|name| {
            let caps = entry_pattern().captures(&name)?;
            let id = caps.get(1)?.as_str().to_string();
            let kind = if caps.get(2)?.as_str().eq_ignore_ascii_case("ARW") {
                "ARW"
            } else {
                "JPG"
            };
            Some((name, id, kind))
        })
        .collect()
}

/// 同じ ID・種別の候補から 1 つを決定的に選ぶ（正規表記を最優先、なければ実名の辞書順で最初）。
fn pick(candidates: &[String], kind: &str, capture_id: &str) -> String {
    let canonical = format!("{capture_id}.{kind}");
    if candidates.contains(&canonical) {
        canonical
    } else {
        candidates.iter().min().cloned().unwrap_or(canonical)
    }
}

/// `capture_id -> 実エントリ名`（種別 `kind` のみ。capture_id 昇順）。
fn list_capture_files(entries: &[(String, String, &'static str)], kind: &str) -> BTreeMap<String, String> {
    let mut grouped: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (name, id, entry_kind) in entries {
        if *entry_kind == kind {
            grouped.entry(id.clone()).or_default().push(name.clone());
        }
    }
    grouped
        .into_iter()
        .map(|(id, names)| {
            let chosen = pick(&names, kind, &id);
            (id, chosen)
        })
        .collect()
}

/// 解析対象の走査結果。
struct Scan {
    /// `capture_id -> JPG のパス`（capture_id 昇順）。
    jpgs: BTreeMap<String, PathBuf>,
    pairs: usize,
    jpg_only: usize,
    arw_only: usize,
    total_bytes: u64,
    /// キャッシュ署名用の実エントリ名一覧（辞書順）。
    names: Vec<String>,
}

fn scan_pairs(root: &Path) -> Scan {
    let all = entries(root);
    let jpg_names = list_capture_files(&all, "JPG");
    let arw_names = list_capture_files(&all, "ARW");

    let mut total_bytes = 0u64;
    for name in jpg_names.values().chain(arw_names.values()) {
        if let Ok(meta) = fs::metadata(root.join(name)) {
            total_bytes += meta.len();
        }
    }
    let pairs = jpg_names.keys().filter(|id| arw_names.contains_key(*id)).count();
    Scan {
        pairs,
        jpg_only: jpg_names.len() - pairs,
        arw_only: arw_names.len() - pairs,
        total_bytes,
        names: all.iter().map(|(name, _, _)| name.clone()).collect(),
        jpgs: jpg_names.into_iter().map(|(id, name)| (id, root.join(name))).collect(),
    }
}

fn resolve_dir(path: &Path) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// 解析キャッシュの署名: 解決済みルートパス + 並べたファイル名一覧の sha1（`_source_signature`）。
fn source_signature(root: &Path, names: &[String]) -> String {
    let resolved = resolve_dir(root);
    let mut payload = resolved.to_string_lossy().into_owned();
    for name in names {
        payload.push('\n');
        payload.push_str(name);
    }
    Sha1::digest(payload.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

/// 表示用のパス（ホーム配下なら `~/...`、それ以外は絶対パス）。Python の `display_path` から
/// プロジェクト相対の分岐を除いたもの（バンドル後は PROJECT_ROOT が無い）。
fn display_path(path: &Path) -> String {
    let resolved = resolve_dir(path);
    if let Some(home) = std::env::var_os("HOME").map(PathBuf::from).map(|h| resolve_dir(&h)) {
        if resolved == home {
            return "~".to_string();
        }
        if let Ok(rest) = resolved.strip_prefix(&home) {
            return format!("~/{}", rest.to_string_lossy());
        }
    }
    resolved.to_string_lossy().into_owned()
}

// ---------------------------------------------------------------------------
// EXIF（kamadak-exif。名指しで読むタグ以外には触れない。GPS は読まない）
// ---------------------------------------------------------------------------

/// グループ化・出力に使う写真 1 枚ぶんの記録。
#[derive(Debug, Clone, PartialEq)]
pub struct PhotoRecord {
    pub capture_id: String,
    pub captured_at: Option<NaiveDateTime>,
    pub iso: Option<i64>,
    pub f_number: Option<f64>,
    pub shutter: Option<f64>,
    pub focal_length: Option<f64>,
    pub exposure_bias: Option<f64>,
    pub lens_model: Option<String>,
    pub orientation: u16,
    pub width: u32,
    pub height: u32,
}

/// EXIF の日時文字列を読む（Python `_parse_datetime` と同じ 4 形式）。
pub fn parse_datetime(raw: &str) -> Option<NaiveDateTime> {
    let text = raw.trim_matches(|c: char| c.is_whitespace() || c == '\0');
    if text.is_empty() {
        return None;
    }
    for pattern in ["%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(text, pattern) {
            return Some(parsed);
        }
        // 秒なしの形式は NaiveDateTime では読めないので、秒 0 を補って試す。
        if !pattern.contains("%S") {
            if let Ok(parsed) = NaiveDateTime::parse_from_str(&format!("{text}:00"), &format!("{pattern}:%S")) {
                return Some(parsed);
            }
        }
    }
    None
}

fn finite(value: f64) -> Option<f64> {
    value.is_finite().then_some(value)
}

fn field_rational(exif: &exif::Exif, tag: exif::Tag) -> Option<f64> {
    let field = exif.get_field(tag, exif::In::PRIMARY)?;
    match &field.value {
        exif::Value::Rational(v) => v.first().and_then(|r| finite(r.to_f64())),
        exif::Value::SRational(v) => v.first().and_then(|r| finite(r.to_f64())),
        _ => None,
    }
}

fn field_ascii(exif: &exif::Exif, tag: exif::Tag) -> Option<String> {
    let field = exif.get_field(tag, exif::In::PRIMARY)?;
    match &field.value {
        exif::Value::Ascii(v) => {
            let bytes = v.first()?;
            let text = String::from_utf8_lossy(bytes);
            let text = text.trim_matches(|c: char| c == '\0').to_string();
            (!text.is_empty()).then_some(text)
        }
        _ => None,
    }
}

/// JPEG から EXIF 項目を読む。EXIF が無い・壊れている場合は項目が None の記録を返す
/// （Python は PIL の `getexif()` が空になるだけで写真自体は残す）。
fn read_exif(path: &Path, capture_id: &str, width: u32, height: u32) -> PhotoRecord {
    let mut record = PhotoRecord {
        capture_id: capture_id.to_string(),
        captured_at: None,
        iso: None,
        f_number: None,
        shutter: None,
        focal_length: None,
        exposure_bias: None,
        lens_model: None,
        orientation: 1,
        width,
        height,
    };
    let Ok(file) = fs::File::open(path) else {
        return record;
    };
    let Ok(exif) = exif::Reader::new().read_from_container(&mut BufReader::new(file)) else {
        return record;
    };
    record.orientation = exif
        .get_field(exif::Tag::Orientation, exif::In::PRIMARY)
        .and_then(|f| f.value.get_uint(0))
        .filter(|v| (1..=8).contains(v))
        .map_or(1, |v| v as u16);
    record.captured_at = field_ascii(&exif, exif::Tag::DateTimeOriginal).and_then(|s| parse_datetime(&s));
    record.iso = exif
        .get_field(exif::Tag::PhotographicSensitivity, exif::In::PRIMARY)
        .and_then(|f| f.value.get_uint(0))
        .map(i64::from);
    record.f_number = field_rational(&exif, exif::Tag::FNumber);
    record.shutter = field_rational(&exif, exif::Tag::ExposureTime);
    record.exposure_bias = field_rational(&exif, exif::Tag::ExposureBiasValue);
    record.focal_length = field_rational(&exif, exif::Tag::FocalLength);
    record.lens_model = field_ascii(&exif, exif::Tag::LensModel);
    record
}

// ---------------------------------------------------------------------------
// デコード + サムネイル
// ---------------------------------------------------------------------------

/// 長辺 640 を保てる最小のスケール（1/1・1/2・1/4・1/8）で JPEG をデコードし、
/// `(元の幅, 元の高さ, スケール後の RGB 画像)` を返す。libjpeg 系の DCT スケールなので
/// 24MP でも全画素のデコードより数倍速い（PIL の `draft` 相当）。
fn decode_scaled(path: &Path) -> Result<(u32, u32, image::RgbImage), String> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut decoder = jpeg_decoder::Decoder::new(BufReader::new(file));
    decoder.read_info().map_err(|e| e.to_string())?;
    let info = decoder.info().ok_or("JPEG ヘッダを読めません")?;
    let (width, height) = (u32::from(info.width), u32::from(info.height));
    decoder.scale(THUMB_MAX_EDGE as u16, THUMB_MAX_EDGE as u16).map_err(|e| e.to_string())?;
    let pixels = decoder.decode().map_err(|e| e.to_string())?;
    let scaled = decoder.info().ok_or("JPEG ヘッダを読めません")?;
    let (sw, sh) = (u32::from(scaled.width), u32::from(scaled.height));
    let rgb = match scaled.pixel_format {
        jpeg_decoder::PixelFormat::RGB24 => pixels,
        jpeg_decoder::PixelFormat::L8 => pixels.iter().flat_map(|&v| [v, v, v]).collect(),
        other => return Err(format!("未対応の JPEG 形式です: {other:?}")),
    };
    let image = image::RgbImage::from_raw(sw, sh, rgb).ok_or("デコード結果の寸法が合いません")?;
    Ok((width, height, image))
}

/// EXIF の Orientation を画素に適用する（PIL `ImageOps.exif_transpose` と同じ向き）。
fn apply_orientation(image: image::RgbImage, orientation: u16) -> image::RgbImage {
    use image::imageops::{flip_horizontal, flip_vertical, rotate180, rotate270, rotate90};
    match orientation {
        2 => flip_horizontal(&image),
        3 => rotate180(&image),
        4 => flip_vertical(&image),
        5 => flip_horizontal(&rotate90(&image)),
        6 => rotate90(&image),
        7 => flip_horizontal(&rotate270(&image)),
        8 => rotate270(&image),
        _ => image,
    }
}

/// 長辺 640 に収め（拡大はしない）、向きを直し、JPEG（q80）にエンコードする。
fn encode_thumbnail(image: image::RgbImage, orientation: u16) -> Result<Vec<u8>, String> {
    let (w, h) = image.dimensions();
    let long = w.max(h);
    let resized = if long > THUMB_MAX_EDGE {
        let scale = f64::from(THUMB_MAX_EDGE) / f64::from(long);
        let nw = ((f64::from(w) * scale).round() as u32).max(1);
        let nh = ((f64::from(h) * scale).round() as u32).max(1);
        image::imageops::resize(&image, nw, nh, image::imageops::FilterType::Lanczos3)
    } else {
        image
    };
    let upright = apply_orientation(resized, orientation);
    let mut out = Vec::new();
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, THUMB_QUALITY)
        .encode_image(&upright)
        .map_err(|e| e.to_string())?;
    Ok(out)
}

fn thumbnail_path(thumbs_dir: &Path, root_key: &str, capture_id: &str) -> PathBuf {
    thumbs_dir.join(format!("{root_key}-{capture_id}.jpg"))
}

/// 1 枚ぶんの EXIF + サムネイル。JPEG をデコードできなければ `Ok(None)`（Python と同じくその写真だけ
/// 飛ばす）。サムネイルの書き込み失敗は写真の不良ではなく環境の失敗なので `Err`。
fn process_photo(
    path: &Path,
    capture_id: &str,
    thumbs_dir: &Path,
    root_key: &str,
) -> Result<Option<PhotoRecord>, CoreImageError> {
    let (width, height, image) = match decode_scaled(path) {
        Ok(decoded) => decoded,
        Err(error) => {
            eprintln!("[raw-analysis] JPEGを読み込めないためスキップします: {capture_id} ({error})");
            return Ok(None);
        }
    };
    let record = read_exif(path, capture_id, width, height);
    let bytes = encode_thumbnail(image, record.orientation)
        .map_err(|e| CoreImageError::new("THUMBNAIL_FAILED", format!("サムネイルを作れません: {capture_id}: {e}")))?;
    let destination = thumbnail_path(thumbs_dir, root_key, capture_id);
    let temporary = destination.with_extension("jpg.tmp");
    fs::write(&temporary, &bytes)
        .and_then(|()| fs::rename(&temporary, &destination))
        .map_err(|e| CoreImageError::new("THUMBNAIL_FAILED", format!("サムネイルを書けません: {}: {e}", destination.display())))?;
    Ok(Some(record))
}

// ---------------------------------------------------------------------------
// グループ化（純関数。Python `group_photos` と同じ結果になる）
// ---------------------------------------------------------------------------

/// kind の表示名（`KIND_LABELS`）。
fn kind_label(kind: &str) -> &'static str {
    match kind {
        "burst" => "連写",
        "bracket" => "ブラケット",
        "similar" => "類似",
        _ => "単写",
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Group {
    pub id: usize,
    pub kind: &'static str,
    pub title: String,
    pub start_at: Option<String>,
    pub end_at: Option<String>,
    pub capture_ids: Vec<String>,
    /// グループの記録（時刻順）。
    pub records: Vec<PhotoRecord>,
}

fn iso_string(value: NaiveDateTime) -> String {
    value.format("%Y-%m-%dT%H:%M:%S").to_string()
}

fn gap_seconds(earlier: NaiveDateTime, later: NaiveDateTime) -> f64 {
    (later - earlier).num_milliseconds() as f64 / 1000.0
}

fn starts_new_group(previous: &PhotoRecord, current: &PhotoRecord) -> bool {
    let (Some(previous_at), Some(current_at)) = (previous.captured_at, current.captured_at) else {
        return true;
    };
    if gap_seconds(previous_at, current_at) > GROUP_GAP_SECONDS {
        return true;
    }
    // `(a or None) != (b or None)`: 空文字は None 扱い。lens_model は読み取り時に空を None にしている。
    if previous.lens_model != current.lens_model {
        return true;
    }
    if let (Some(a), Some(b)) = (previous.focal_length, current.focal_length) {
        if a > 0.0 && b > 0.0 && a.max(b) / a.min(b) > GROUP_FOCAL_RATIO {
            return true;
        }
    }
    false
}

fn is_bracket(records: &[PhotoRecord]) -> bool {
    let values: Vec<f64> = records.iter().filter_map(|r| r.exposure_bias).collect();
    let mut distinct: Vec<i64> = values.iter().map(|v| (v * 1000.0).round() as i64).collect();
    distinct.sort_unstable();
    distinct.dedup();
    if distinct.len() < BRACKET_MIN_DISTINCT_BIAS {
        return false;
    }
    let max = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let min = values.iter().cloned().fold(f64::INFINITY, f64::min);
    (max - min) >= BRACKET_MIN_SPAN_EV - BRACKET_SPAN_EPSILON
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(|a, b| a.total_cmp(b));
    let n = values.len();
    if n % 2 == 1 {
        values[n / 2]
    } else {
        (values[n / 2 - 1] + values[n / 2]) / 2.0
    }
}

fn classify_kind(records: &[PhotoRecord]) -> &'static str {
    if records.len() < 2 {
        return "single";
    }
    // ブラケットは連写より優先する。
    if is_bracket(records) {
        return "bracket";
    }
    let mut gaps: Vec<f64> = records
        .windows(2)
        .filter_map(|w| Some(gap_seconds(w[0].captured_at?, w[1].captured_at?)))
        .collect();
    if !gaps.is_empty() && median(&mut gaps) <= BURST_MEDIAN_GAP_SECONDS {
        return "burst";
    }
    "similar"
}

fn finalize_group(records: Vec<PhotoRecord>, index: usize) -> Group {
    let kind = classify_kind(&records);
    let known: Vec<NaiveDateTime> = records.iter().filter_map(|r| r.captured_at).collect();
    let clock = known.first().map_or("--:--".to_string(), |t| t.format("%H:%M").to_string());
    Group {
        id: index,
        kind,
        title: format!("{clock} {} {}枚", kind_label(kind), records.len()),
        start_at: known.first().map(|t| iso_string(*t)),
        end_at: known.last().map(|t| iso_string(*t)),
        capture_ids: records.iter().map(|r| r.capture_id.clone()).collect(),
        records,
    }
}

/// 撮影時刻の隙間・レンズ変更・焦点距離比で連続する写真を分ける。
/// 時刻なしは最後尾、同時刻は capture_id 順（`_sort_key`）。
pub fn group_photos(mut records: Vec<PhotoRecord>) -> Vec<Group> {
    records.sort_by(|a, b| {
        (a.captured_at.is_none(), a.captured_at, &a.capture_id).cmp(&(b.captured_at.is_none(), b.captured_at, &b.capture_id))
    });
    let mut groups: Vec<Group> = Vec::new();
    let mut current: Vec<PhotoRecord> = Vec::new();
    for record in records {
        if current.last().is_some_and(|last| starts_new_group(last, &record)) {
            let finished = std::mem::take(&mut current);
            groups.push(finalize_group(finished, groups.len()));
        }
        current.push(record);
    }
    if !current.is_empty() {
        groups.push(finalize_group(current, groups.len()));
    }
    groups
}

// ---------------------------------------------------------------------------
// RAW 推薦（EXIF 由来の分だけ）
// ---------------------------------------------------------------------------

fn raw_recommendation_lite(group_kind: &str, representative: &PhotoRecord) -> Value {
    let iso = representative.iso.unwrap_or(0);
    let bias = representative.exposure_bias;
    let mut codes: Vec<&str> = Vec::new();
    let mut reasons: Vec<String> = Vec::new();

    if iso >= RAW_RECOMMENDED_ISO {
        codes.push("high_iso");
        reasons.push(format!("ISO {iso} と高感度のため、ノイズ低減の効果が大きい写真です。"));
    }
    if group_kind == "bracket" {
        codes.push("exposure_bracket");
        reasons.push("露出を変えて撮影されています。RAWでの合成・選択に向いています。".to_string());
    }
    let low_bias = bias.is_some_and(|b| b <= RAW_MAYBE_EXPOSURE_BIAS);
    let level = if iso >= RAW_RECOMMENDED_ISO {
        "recommended"
    } else if low_bias {
        "maybe"
    } else {
        "none"
    };
    if level == "maybe" && codes.is_empty() {
        if let Some(b) = bias {
            reasons.push(format!("露出を {b:+.1}EV 下げて撮影されています。RAWなら明るさを作り直せます。"));
        }
    }
    if level == "none" && reasons.is_empty() {
        // Python の文言は白飛び・黒つぶれを測った結果を述べるが、lite は測っていないので言い換える。
        reasons.push("撮影情報からは、RAW現像を特に勧める要素は見つかりませんでした（画像の採点は未実施）。".to_string());
    }
    let label = match level {
        "strong" => "RAW現像を強く推奨",
        "recommended" => "RAW現像がおすすめ",
        "maybe" => "RAW現像の余地あり",
        _ => "JPEGで十分",
    };
    json!({ "level": level, "label": label, "reason_codes": codes, "reasons": reasons })
}

// ---------------------------------------------------------------------------
// 全体の組み立て
// ---------------------------------------------------------------------------

/// 進捗（ポーリング用）。
#[derive(Default)]
pub struct Progress {
    stage: Mutex<String>,
    done: AtomicUsize,
    total: AtomicUsize,
}

impl Progress {
    fn set_stage(&self, stage: &str) {
        *self.stage.lock().unwrap_or_else(|p| p.into_inner()) = stage.to_string();
    }
    pub fn snapshot(&self) -> Value {
        let stage = self.stage.lock().unwrap_or_else(|p| p.into_inner()).clone();
        json!({ "stage": stage, "done": self.done.load(Ordering::Relaxed), "total": self.total.load(Ordering::Relaxed) })
    }
}

fn opt_f64(value: Option<f64>) -> Value {
    value.map_or(Value::Null, |v| json!(v))
}

fn photo_json(record: &PhotoRecord, thumb: &Path) -> Value {
    json!({
        "capture_id": record.capture_id,
        "captured_at": record.captured_at.map(iso_string),
        "iso": record.iso,
        "f_number": opt_f64(record.f_number),
        "shutter": opt_f64(record.shutter),
        "focal_length": opt_f64(record.focal_length),
        "exposure_bias": opt_f64(record.exposure_bias),
        "lens_model": record.lens_model,
        "orientation": record.orientation,
        "width": record.width,
        "height": record.height,
        // 採点は N-C2。lite は 0 固定（`scored: false` で判別できる）。
        "sharpness_raw": 0,
        "sharpness_score": 0,
        "highlight_clip_ratio": 0,
        "shadow_clip_ratio": 0,
        "mean_luma": 0,
        "overall_score": 0,
        "thumb": thumb.to_string_lossy(),
    })
}

/// 解析を最後まで走らせ、`Analysis` JSON（`analysis_version` 3）を返す。
pub fn build_analysis(
    root: &Path,
    thumbs_dir: &Path,
    root_key: &str,
    progress: &Progress,
) -> Result<Value, CoreImageError> {
    progress.set_stage("scanning");
    let scan = scan_pairs(root);
    progress.total.store(scan.jpgs.len(), Ordering::Relaxed);
    fs::create_dir_all(thumbs_dir).map_err(|e| {
        CoreImageError::new("THUMBNAIL_FAILED", format!("サムネイルのフォルダを作れません: {}: {e}", thumbs_dir.display()))
    })?;

    progress.set_stage("scoring");
    let jobs: Vec<(&String, &PathBuf)> = scan.jpgs.iter().collect();
    let next = AtomicUsize::new(0);
    let results: Mutex<Vec<Result<Option<PhotoRecord>, CoreImageError>>> = Mutex::new(Vec::new());
    std::thread::scope(|scope| {
        for _ in 0..MAX_WORKERS.min(jobs.len().max(1)) {
            scope.spawn(|| loop {
                let index = next.fetch_add(1, Ordering::Relaxed);
                let Some((id, path)) = jobs.get(index) else { break };
                let outcome = process_photo(path, id, thumbs_dir, root_key);
                progress.done.fetch_add(1, Ordering::Relaxed);
                results.lock().unwrap_or_else(|p| p.into_inner()).push(outcome);
            });
        }
    });
    let mut records = Vec::new();
    for outcome in results.into_inner().unwrap_or_else(|p| p.into_inner()) {
        if let Some(record) = outcome? {
            records.push(record);
        }
    }

    progress.set_stage("grouping");
    let groups = group_photos(records);
    let mut photos = Map::new();
    let mut group_infos = Vec::new();
    let mut raw_candidates = 0;
    for group in &groups {
        // 採点しないので代表は時刻順の先頭。
        let representative = &group.records[0];
        let raw = raw_recommendation_lite(group.kind, representative);
        if matches!(raw["level"].as_str(), Some("recommended" | "strong")) {
            raw_candidates += 1;
        }
        for record in &group.records {
            let thumb = thumbnail_path(thumbs_dir, root_key, &record.capture_id);
            photos.insert(record.capture_id.clone(), photo_json(record, &thumb));
        }
        group_infos.push(json!({
            "id": group.id,
            "kind": group.kind,
            "title": group.title,
            "start_at": group.start_at,
            "end_at": group.end_at,
            "capture_ids": group.capture_ids,
            "recommended_capture_id": representative.capture_id,
            "confidence": LITE_CONFIDENCE,
            "recommendation_reasons": [],
            "raw": raw,
        }));
    }

    Ok(json!({
        "analysis_version": ANALYSIS_VERSION,
        "scored": false,
        "generated_at": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S%:z").to_string(),
        "source": {
            "root": display_path(root),
            "pairs": scan.pairs,
            "jpg_only": scan.jpg_only,
            "arw_only": scan.arw_only,
            "total_bytes": scan.total_bytes,
        },
        "summary": { "groups": group_infos.len(), "raw_candidates": raw_candidates, "needs_review": 0 },
        "photos": photos,
        "groups": group_infos,
        "_source_signature": source_signature(root, &scan.names),
    }))
}

// ---------------------------------------------------------------------------
// キャッシュ
// ---------------------------------------------------------------------------

const SIGNATURE_KEY: &str = "_source_signature";

fn analysis_dir(app_data_dir: &Path) -> PathBuf {
    app_data_dir.join("analysis")
}

fn thumbnails_dir(app_data_dir: &Path) -> PathBuf {
    app_data_dir.join("thumbnails")
}

fn cache_path(app_data_dir: &Path, root_key: &str) -> PathBuf {
    analysis_dir(app_data_dir).join(format!("{root_key}.json"))
}

fn write_cache(path: &Path, payload: &Value) -> Result<(), CoreImageError> {
    let fail = |e: std::io::Error| CoreImageError::new("CACHE_WRITE_FAILED", format!("解析結果を保存できません: {}: {e}", path.display()));
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(fail)?;
    }
    let temporary = path.with_extension("json.tmp");
    let text = serde_json::to_string_pretty(payload).map_err(|e| CoreImageError::new("CACHE_WRITE_FAILED", e.to_string()))?;
    fs::write(&temporary, text + "\n").and_then(|()| fs::rename(&temporary, path)).map_err(fail)
}

/// キャッシュが有効（版・署名が一致し、サムネイルが揃っている）なら署名を除いて返す。
fn read_cached_analysis(app_data_dir: &Path, root: &Path) -> Option<Value> {
    let root_key = root_key_for(root);
    let text = fs::read_to_string(cache_path(app_data_dir, &root_key)).ok()?;
    let mut payload: Value = serde_json::from_str(&text).ok()?;
    let object = payload.as_object_mut()?;
    if object.get("analysis_version").and_then(Value::as_u64) != Some(u64::from(ANALYSIS_VERSION)) {
        return None;
    }
    let signature = object.remove(SIGNATURE_KEY)?;
    if signature.as_str() != Some(source_signature(root, &scan_names(root)).as_str()) {
        return None;
    }
    // サムネイルが消えている（掃除された・APPDATA が移った）ときは作り直す。
    let photos = object.get("photos")?.as_object()?;
    let intact = photos
        .values()
        .all(|p| p.get("thumb").and_then(Value::as_str).is_some_and(|t| Path::new(t).is_file()));
    intact.then_some(payload)
}

fn scan_names(root: &Path) -> Vec<String> {
    entries(root).into_iter().map(|(name, _, _)| name).collect()
}

// ---------------------------------------------------------------------------
// ジョブ状態と Tauri コマンド
// ---------------------------------------------------------------------------

#[derive(Default)]
struct Inner {
    running: Option<Arc<Progress>>,
    last_error: Option<String>,
}

/// 解析ジョブの状態（Tauri の managed state）。
#[derive(Default, Clone)]
pub struct AnalysisState(Arc<Mutex<Inner>>);

impl AnalysisState {
    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.0.lock().unwrap_or_else(|p| p.into_inner())
    }
}

fn app_data_dir<R: Runtime>(app: &AppHandle<R>) -> Result<PathBuf, CoreImageError> {
    app.path().app_data_dir().map_err(|error| {
        CoreImageError::new("APP_DATA_DIR_UNAVAILABLE", format!("アプリのデータフォルダを取得できません: {error}"))
    })
}

fn blocking_failed(error: tauri::Error) -> CoreImageError {
    CoreImageError::new("INTERNAL_ERROR", format!("解析スレッドが異常終了しました: {error}"))
}

/// `invoke("get_analysis")` → `{status: "absent"|"running"|"ready", ...}`。
/// 現像元フォルダが未設定なら `SOURCE_ROOT_NOT_CONFIGURED` で reject する。
#[tauri::command]
pub async fn get_analysis(app: AppHandle, state: tauri::State<'_, AnalysisState>) -> Result<Value, CoreImageError> {
    let state = state.inner().clone();
    let data_dir = app_data_dir(&app)?;
    tauri::async_runtime::spawn_blocking(move || {
        let progress = state.lock().running.clone();
        if let Some(progress) = progress {
            return Ok(json!({ "status": "running", "progress": progress.snapshot() }));
        }
        let root = read_source_root(&data_dir)?;
        if let Some(analysis) = read_cached_analysis(&data_dir, &root) {
            return Ok(json!({ "status": "ready", "analysis": analysis }));
        }
        let mut absent = json!({ "status": "absent" });
        if let Some(error) = state.lock().last_error.clone() {
            absent["error"] = json!(error);
        }
        Ok(absent)
    })
    .await
    .map_err(blocking_failed)?
}

/// 実行中の印を、ジョブの終了（正常・失敗・panic）で必ず外す。
struct RunningGuard(AnalysisState);

impl Drop for RunningGuard {
    fn drop(&mut self) {
        self.0.lock().running = None;
    }
}

/// `invoke("start_analysis", { force })` → `"started" | "already_running" | "ready"`。
/// 解析本体は `spawn_blocking` で走らせ、このコマンドはすぐ返る。進捗は `get_analysis` で読む。
#[tauri::command]
pub async fn start_analysis(
    app: AppHandle,
    state: tauri::State<'_, AnalysisState>,
    force: Option<bool>,
) -> Result<&'static str, CoreImageError> {
    let force = force.unwrap_or(false);
    let state = state.inner().clone();
    let data_dir = app_data_dir(&app)?;
    tauri::async_runtime::spawn_blocking(move || {
        let root = resolve_dir(&read_source_root(&data_dir)?);
        let progress = {
            let mut inner = state.lock();
            if inner.running.is_some() {
                return Ok("already_running");
            }
            if !force && read_cached_analysis(&data_dir, &root).is_some() {
                return Ok("ready");
            }
            let progress = Arc::new(Progress::default());
            progress.set_stage("scanning");
            inner.running = Some(progress.clone());
            inner.last_error = None;
            progress
        };
        let job_state = state.clone();
        tauri::async_runtime::spawn_blocking(move || {
            let _guard = RunningGuard(job_state.clone());
            let outcome = run_analysis(&data_dir, &root, &progress);
            job_state.lock().last_error = outcome.err().map(|e| e.message);
        });
        Ok("started")
    })
    .await
    .map_err(blocking_failed)?
}

fn run_analysis(data_dir: &Path, root: &Path, progress: &Progress) -> Result<(), CoreImageError> {
    let root_key = root_key_for(root);
    let payload = build_analysis(root, &thumbnails_dir(data_dir), &root_key, progress)?;
    write_cache(&cache_path(data_dir, &root_key), &payload)
}

// ---------------------------------------------------------------------------
// 元 JPG の配信（URI scheme `geoframe`。案 2）
// ---------------------------------------------------------------------------

fn source_jpg_name_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"^/source-jpg/(DSC[0-9]{5})\.JPG$").expect("valid regex"))
}

fn plain_response(status: u16, message: &str) -> Response<Vec<u8>> {
    Response::builder()
        .status(status)
        .header(http::header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .header(http::header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
        .body(message.as_bytes().to_vec())
        .expect("static response")
}

/// `/source-jpg/DSC12345.JPG` を現像元フォルダから解決して返す（`server.py` の `handle_source_jpg` と
/// 同じ検証: ファイル名は `DSC` + ASCII 5 桁 + `.JPG` 固定、`resolve()` 後の親が現像元と一致、通常ファイル）。
/// 実ファイルの拡張子は `.jpg` でもよい。
pub fn serve_source_jpg(app_data_dir: &Path, url_path: &str) -> Response<Vec<u8>> {
    let Some(caps) = source_jpg_name_pattern().captures(url_path) else {
        return plain_response(400, "無効なJPGファイル名です。");
    };
    let capture_id = &caps[1];
    if !is_valid_capture_id(capture_id) {
        return plain_response(400, "無効なJPGファイル名です。");
    }
    let Ok(root) = read_source_root(app_data_dir) else {
        return plain_response(404, "現像元フォルダが未設定です。");
    };
    let root = resolve_dir(&root);
    let names: Vec<String> = entries(&root)
        .into_iter()
        .filter(|(_, id, kind)| id == capture_id && *kind == "JPG")
        .map(|(name, _, _)| name)
        .collect();
    if names.is_empty() {
        return plain_response(404, "元JPGが見つかりません。");
    }
    let chosen = pick(&names, "JPG", capture_id);
    let Ok(resolved) = fs::canonicalize(root.join(chosen)) else {
        return plain_response(404, "元JPGが見つかりません。");
    };
    // シンボリックリンクによる現像元フォルダ外の参照を拒否する。
    if resolved.parent() != Some(root.as_path()) || !resolved.is_file() {
        return plain_response(404, "元JPGが見つかりません。");
    }
    match fs::read(&resolved) {
        Ok(bytes) => Response::builder()
            .status(200)
            .header(http::header::CONTENT_TYPE, "image/jpeg")
            .header(http::header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
            .body(bytes)
            .expect("static response"),
        Err(_) => plain_response(500, "元JPGを読めません。"),
    }
}

/// `register_asynchronous_uri_scheme_protocol("geoframe", ...)` に渡すハンドラ。
/// 大きな JPEG（24MP で約 10MB）の読み込みで UI スレッドを塞がないよう、別スレッドで応答する。
pub fn source_jpg_protocol<R: Runtime>(
    ctx: UriSchemeContext<'_, R>,
    request: Request<Vec<u8>>,
    responder: UriSchemeResponder,
) {
    let data_dir = ctx.app_handle().path().app_data_dir();
    let path = request.uri().path().to_string();
    std::thread::spawn(move || {
        let response = match data_dir {
            Ok(dir) => serve_source_jpg(&dir, &path),
            Err(_) => plain_response(500, "アプリのデータフォルダを取得できません。"),
        };
        responder.respond(response);
    });
}

// ---------------------------------------------------------------------------
// テスト
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(text: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S").unwrap()
    }

    fn rec(id: &str, at: &str, focal: f64, bias: f64, lens: Option<&str>) -> PhotoRecord {
        PhotoRecord {
            capture_id: id.to_string(),
            captured_at: Some(dt(at)),
            iso: Some(100),
            f_number: Some(4.0),
            shutter: Some(0.01),
            focal_length: Some(focal),
            exposure_bias: Some(bias),
            lens_model: lens.map(str::to_string),
            orientation: 1,
            width: 6000,
            height: 4000,
        }
    }

    fn simple(id: &str, at: &str) -> PhotoRecord {
        rec(id, at, 16.0, 0.0, Some("lens"))
    }

    // --- grouping（tests/test_analysis.py と同じ事例） ---

    #[test]
    fn gap_over_sixty_seconds_splits() {
        let g = group_photos(vec![simple("DSC00001", "2025-06-07T10:00:00"), simple("DSC00002", "2025-06-07T10:01:01")]);
        assert_eq!(g.len(), 2);
    }

    #[test]
    fn gap_exactly_sixty_seconds_stays() {
        let g = group_photos(vec![simple("DSC00001", "2025-06-07T10:00:00"), simple("DSC00002", "2025-06-07T10:01:00")]);
        assert_eq!(g.len(), 1);
    }

    #[test]
    fn lens_change_splits() {
        let g = group_photos(vec![
            rec("DSC00001", "2025-06-07T10:00:00", 16.0, 0.0, Some("a")),
            rec("DSC00002", "2025-06-07T10:00:01", 16.0, 0.0, Some("b")),
        ]);
        assert_eq!(g.len(), 2);
    }

    #[test]
    fn focal_ratio_splits_only_above_threshold() {
        let split = group_photos(vec![
            rec("DSC00001", "2025-06-07T10:00:00", 10.0, 0.0, None),
            rec("DSC00002", "2025-06-07T10:00:01", 15.5, 0.0, None),
        ]);
        assert_eq!(split.len(), 2);
        let stay = group_photos(vec![
            rec("DSC00001", "2025-06-07T10:00:00", 10.0, 0.0, None),
            rec("DSC00002", "2025-06-07T10:00:01", 15.0, 0.0, None),
        ]);
        assert_eq!(stay.len(), 1);
    }

    #[test]
    fn same_second_sorts_by_capture_id() {
        let g = group_photos(vec![simple("DSC00009", "2025-06-07T10:00:00"), simple("DSC00002", "2025-06-07T10:00:00")]);
        assert_eq!(g[0].capture_ids, vec!["DSC00002", "DSC00009"]);
    }

    #[test]
    fn missing_time_sorts_last_and_splits() {
        let mut undated = simple("DSC00001", "2025-06-07T10:00:00");
        undated.captured_at = None;
        let g = group_photos(vec![undated, simple("DSC00002", "2025-06-07T10:00:00")]);
        assert_eq!(g.len(), 2);
        assert_eq!(g[0].capture_ids, vec!["DSC00002"]);
        assert_eq!(g[1].capture_ids, vec!["DSC00001"]);
        assert_eq!(g[1].title, "--:-- 単写 1枚");
        assert_eq!(g[1].start_at, None);
    }

    #[test]
    fn burst_single_and_similar_kinds() {
        let burst = group_photos((0..4).map(|i| simple(&format!("DSC0000{i}"), &format!("2025-06-07T10:00:0{i}"))).collect());
        assert_eq!((burst.len(), burst[0].kind), (1, "burst"));
        assert_eq!(burst[0].title, "10:00 連写 4枚");
        assert_eq!(group_photos(vec![simple("DSC00001", "2025-06-07T10:00:00")])[0].kind, "single");
        let similar = group_photos(vec![simple("DSC00001", "2025-06-07T10:00:00"), simple("DSC00002", "2025-06-07T10:00:08")]);
        assert_eq!(similar[0].kind, "similar");
    }

    fn with_bias(biases: &[f64]) -> Vec<PhotoRecord> {
        biases
            .iter()
            .enumerate()
            .map(|(i, b)| rec(&format!("DSC0000{i}"), &format!("2025-06-07T10:00:0{i}"), 16.0, *b, None))
            .collect()
    }

    #[test]
    fn constant_bias_is_not_bracket() {
        assert_eq!(group_photos(with_bias(&[-0.3, -0.3, -0.3]))[0].kind, "burst");
        assert_ne!(group_photos(with_bias(&[0.3, 0.3]))[0].kind, "bracket");
    }

    #[test]
    fn wide_bias_is_bracket_and_wins_over_burst() {
        assert_eq!(group_photos(with_bias(&[-1.0, 0.0, 1.0]))[0].kind, "bracket");
        assert_eq!(group_photos(with_bias(&[-1.0, 1.0]))[0].kind, "bracket");
    }

    #[test]
    fn narrow_span_is_not_bracket_but_exact_threshold_counts() {
        assert_ne!(group_photos(with_bias(&[-0.3, 0.0]))[0].kind, "bracket");
        assert!(is_bracket(&with_bias(&[-0.3, 0.3])));
        // EXIF の有理数 -3/10, 3/10 を f64 にしても 0.6 ちょうどで拾える。
        assert!(is_bracket(&with_bias(&[-3.0 / 10.0, 3.0 / 10.0])));
    }

    #[test]
    fn median_even_and_odd() {
        assert_eq!(median(&mut [3.0, 1.0, 2.0]), 2.0);
        assert_eq!(median(&mut [4.0, 1.0, 2.0, 3.0]), 2.5);
    }

    #[test]
    fn parse_datetime_formats() {
        assert_eq!(parse_datetime("2025:06:07 10:00:01\0"), Some(dt("2025-06-07T10:00:01")));
        assert_eq!(parse_datetime("2025-06-07 10:00:01"), Some(dt("2025-06-07T10:00:01")));
        assert_eq!(parse_datetime("2025:06:07 10:00"), Some(dt("2025-06-07T10:00:00")));
        assert_eq!(parse_datetime("2025-06-07T10:00:01"), Some(dt("2025-06-07T10:00:01")));
        assert_eq!(parse_datetime("  "), None);
        assert_eq!(parse_datetime("garbage"), None);
    }

    // --- RAW 推薦 lite ---

    #[test]
    fn raw_recommendation_uses_only_exif() {
        let mut r = simple("DSC00001", "2025-06-07T10:00:00");
        assert_eq!(raw_recommendation_lite("single", &r)["level"], "none");
        r.iso = Some(1600);
        let high = raw_recommendation_lite("single", &r);
        assert_eq!((high["level"].as_str(), high["reason_codes"].clone()), (Some("recommended"), json!(["high_iso"])));
        r.iso = Some(100);
        r.exposure_bias = Some(-1.0);
        assert_eq!(raw_recommendation_lite("single", &r)["level"], "maybe");
        let bracket = raw_recommendation_lite("bracket", &simple("DSC00001", "2025-06-07T10:00:00"));
        assert_eq!(bracket["reason_codes"], json!(["exposure_bracket"]));
    }

    // --- 走査 ---

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("geoframe-analysis-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn scan_counts_pairs_and_ignores_other_names() {
        let dir = temp_dir("scan");
        for name in ["DSC00001.JPG", "DSC00001.ARW", "DSC00002.jpg", "DSC00003.arw", "DSC0004.JPG", "notes.txt", "DSC00005.JPG.bak"] {
            fs::write(dir.join(name), b"x").unwrap();
        }
        let scan = scan_pairs(&dir);
        assert_eq!(scan.jpgs.keys().collect::<Vec<_>>(), ["DSC00001", "DSC00002"]);
        assert_eq!((scan.pairs, scan.jpg_only, scan.arw_only), (1, 1, 1));
        assert_eq!(scan.total_bytes, 4);
        assert_eq!(scan.names.len(), 4);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn signature_changes_with_names() {
        let dir = temp_dir("sig");
        let a = source_signature(&dir, &["DSC00001.JPG".to_string()]);
        let b = source_signature(&dir, &["DSC00001.JPG".to_string(), "DSC00002.JPG".to_string()]);
        assert_ne!(a, b);
        let _ = fs::remove_dir_all(&dir);
    }

    // --- デコード・サムネイル・EXIF（合成 JPEG） ---

    fn synthetic_jpeg(path: &Path, w: u32, h: u32) {
        let image = image::RgbImage::from_fn(w, h, |x, y| image::Rgb([(x % 256) as u8, (y % 256) as u8, 128]));
        let mut out = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, 90).encode_image(&image).unwrap();
        fs::write(path, out).unwrap();
    }

    #[test]
    fn thumbnail_long_edge_is_640_and_orientation_applied() {
        let dir = temp_dir("thumb");
        let path = dir.join("DSC00001.JPG");
        synthetic_jpeg(&path, 3000, 2000);
        let (w, h, image) = decode_scaled(&path).unwrap();
        assert_eq!((w, h), (3000, 2000));
        assert!(image.width() >= 640 && image.width() < 3000);
        let landscape = image::load_from_memory(&encode_thumbnail(image.clone(), 1).unwrap()).unwrap();
        assert_eq!((landscape.width(), landscape.height()), (640, 427));
        let portrait = image::load_from_memory(&encode_thumbnail(image, 6).unwrap()).unwrap();
        assert_eq!((portrait.width(), portrait.height()), (427, 640));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn small_image_is_not_upscaled_and_corrupt_is_skipped() {
        let dir = temp_dir("small");
        let small = dir.join("DSC00001.JPG");
        synthetic_jpeg(&small, 200, 100);
        let thumbs = dir.join("t");
        fs::create_dir_all(&thumbs).unwrap();
        let record = process_photo(&small, "DSC00001", &thumbs, "abcd1234").unwrap().unwrap();
        assert_eq!((record.width, record.height, record.orientation), (200, 100, 1));
        let decoded = image::open(thumbs.join("abcd1234-DSC00001.jpg")).unwrap();
        assert_eq!((decoded.width(), decoded.height()), (200, 100));
        let broken = dir.join("DSC00002.JPG");
        fs::write(&broken, b"not a jpeg").unwrap();
        assert!(process_photo(&broken, "DSC00002", &thumbs, "abcd1234").unwrap().is_none());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn build_analysis_end_to_end_with_cache() {
        let dir = temp_dir("e2e");
        let root = dir.join("src");
        fs::create_dir_all(&root).unwrap();
        for id in ["DSC00001", "DSC00002"] {
            synthetic_jpeg(&root.join(format!("{id}.JPG")), 800, 600);
            fs::write(root.join(format!("{id}.ARW")), b"raw").unwrap();
        }
        let data = dir.join("data");
        let key = root_key_for(&root);
        let payload = build_analysis(&root, &thumbnails_dir(&data), &key, &Progress::default()).unwrap();
        assert_eq!(payload["analysis_version"], 3);
        assert_eq!(payload["scored"], false);
        assert_eq!(payload["source"]["pairs"], 2);
        assert_eq!(payload["photos"]["DSC00001"]["sharpness_raw"], 0);
        assert_eq!(payload["photos"]["DSC00001"]["captured_at"], Value::Null);
        // 時刻なしなので 2 枚は別グループ。
        assert_eq!(payload["summary"]["groups"], 2);
        assert_eq!(payload["groups"][0]["confidence"], 0.5);
        write_cache(&cache_path(&data, &key), &payload).unwrap();
        let cached = read_cached_analysis(&data, &root).expect("cache valid");
        assert!(cached.get(SIGNATURE_KEY).is_none());
        // ファイル名一覧が変わると無効。
        fs::write(root.join("DSC00003.JPG"), b"x").unwrap();
        assert!(read_cached_analysis(&data, &root).is_none());
        let _ = fs::remove_dir_all(&dir);
    }

    // --- 元 JPG の配信 ---

    #[test]
    fn serve_source_jpg_validates_names_and_root() {
        let dir = temp_dir("serve");
        let root = dir.join("src");
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("DSC00001.jpg"), b"jpegbytes").unwrap();
        fs::write(root.join("secret.txt"), b"secret").unwrap();
        let data = dir.join("data");
        fs::create_dir_all(&data).unwrap();
        fs::write(data.join("config.json"), json!({ "source_root": root.to_string_lossy() }).to_string()).unwrap();

        let ok = serve_source_jpg(&data, "/source-jpg/DSC00001.JPG");
        assert_eq!((ok.status().as_u16(), ok.body().as_slice()), (200, b"jpegbytes".as_slice()));
        for bad in [
            "/source-jpg/DSC0001.JPG",
            "/source-jpg/DSC00001.jpg",
            "/source-jpg/../secret.txt",
            "/source-jpg/DSC00001.JPG/x",
            "/source-jpg/DSC０００１.JPG",
            "/other/DSC00001.JPG",
        ] {
            assert_eq!(serve_source_jpg(&data, bad).status().as_u16(), 400, "{bad}");
        }
        assert_eq!(serve_source_jpg(&data, "/source-jpg/DSC00009.JPG").status().as_u16(), 404);
        // フォルダ外を指すシンボリックリンクは返さない。
        let outside = dir.join("outside.jpg");
        fs::write(&outside, b"outside").unwrap();
        std::os::unix::fs::symlink(&outside, root.join("DSC00002.JPG")).unwrap();
        assert_eq!(serve_source_jpg(&data, "/source-jpg/DSC00002.JPG").status().as_u16(), 404);
        let _ = fs::remove_dir_all(&dir);
    }

    // --- 実機フォルダの出力（手動。`GEOFRAME_ANALYSIS_DIR` / `GEOFRAME_ANALYSIS_OUT` を指定して `--ignored`） ---

    #[test]
    #[ignore]
    fn dump_real_folder() {
        let root = PathBuf::from(std::env::var("GEOFRAME_ANALYSIS_DIR").expect("GEOFRAME_ANALYSIS_DIR"));
        let out = PathBuf::from(std::env::var("GEOFRAME_ANALYSIS_OUT").expect("GEOFRAME_ANALYSIS_OUT"));
        let thumbs = out.join("thumbnails");
        let started = std::time::Instant::now();
        let payload = build_analysis(&root, &thumbs, &root_key_for(&root), &Progress::default()).unwrap();
        eprintln!("elapsed: {:?} for {} photos", started.elapsed(), payload["photos"].as_object().unwrap().len());
        fs::write(out.join("analysis-rust.json"), serde_json::to_string_pretty(&payload).unwrap()).unwrap();
        // 1 枚ずつ（並列なし）のデコード + エンコード時間。
        let mut total = std::time::Duration::ZERO;
        let mut count = 0;
        for (_, path) in scan_pairs(&root).jpgs {
            let t = std::time::Instant::now();
            let (_, _, image) = decode_scaled(&path).unwrap();
            let decode = t.elapsed();
            encode_thumbnail(image, 1).unwrap();
            let all = t.elapsed();
            eprintln!("{}: decode {:?}, decode+thumb {:?}", path.file_name().unwrap().to_string_lossy(), decode, all);
            total += all;
            count += 1;
        }
        eprintln!("mean per photo (serial): {:?}", total / count);
    }
}
