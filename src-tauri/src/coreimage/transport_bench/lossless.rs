//! #141 追加対照: **可逆出力（tiff16）**で単発 CLI と batch の画素差を測る（test 専用・実素材は ignored）。
//!
//! 背景: JPEG ベンチの `batch_decode_render` は single に対して max 8〜10/255・mean 0.29 の差を示した。
//! README にある既知の差は 640px TIFF16 の最大 34〜81/65535（デコード結果を half float で保持する丸め）で、
//! 単位が違う（JPEG は 8bit + 量子化 + 色変換を挟む）。この差を **品質の合否にはしない**（閾値は未定義）。
//! 本モジュールは JPEG を介さず、16bit RGB を **8bit に落とさず**そのまま比べる。
//!
//! 既存の JPEG ベンチ（`run_bench`）の契約・意味は変えない。リクエストは同じ 9 キー・2400px・レンズ補正 ON・
//! decoder 9・apple_default で、`format: tiff16`・`output_color_space: srgb`（JPEG と同じ sRGB 域）だけが違う
//! （tiff16 に `jpeg_quality` は送れない）。
//!
//! 比較の組（基準は単発 CLI の 1 回目 = A）:
//! - `single_repeat`: 単発 CLI の 2 回目 vs A。**決定性の対照**（0 でなければ差の解釈に使えない）。
//! - `batch_stateless`: batch の `op` 無し現像 vs A。**輸送だけの対照**（同じ経路なので画素一致の見込み）。
//! - `batch_decode_render`: `op: decode` → `op: render` vs A。RGBAh 保持（半精度）由来の差が出る組。
//!
//! 公開 JSON にはパス・GPS・画像の値（画素列）を書かない。数値の要約（max/mean/異画素数/ヒストグラム）のみ。
use super::*;

pub(super) const FORMAT: &str = "tiff16";
pub(super) const OUTPUT_COLOR_SPACE: &str = "srgb";
const CHANNELS: [&str; 3] = ["r", "g", "b"];

pub(super) struct LosslessOptions {
    /// 比較後に TIFF（約 23MB/枚）を残す。既定は消す。
    pub(super) keep_images: bool,
}

// ---------------------------------------------------------------------------
// 最小の TIFF16 リーダー（image クレートは tiff を持たない。依存は足さない）
// ---------------------------------------------------------------------------

#[derive(Debug)]
#[allow(dead_code)]
pub(super) struct Tiff16 {
    pub(super) width: u32,
    pub(super) height: u32,
    /// RGB インターリーブの 16bit サンプル（`width*height*3`）。
    pub(super) samples: Vec<u16>,
    pub(super) big_endian: bool,
}

struct Entry {
    ty: u16,
    count: u32,
    /// 値そのもの（4byte 以内）か、値へのオフセット。
    raw: [u8; 4],
}

/// 非圧縮・16bit・RGB（アルファ無し）・チャンク（strip）形式の TIFF だけを読む。
/// それ以外（圧縮・BigTIFF・予測子・8bit・追加サンプル・タイル）は理由付きで Err にし、**推測で読まない**。
pub(super) fn read_tiff16(path: &Path) -> Result<Tiff16, String> {
    let bytes = fs::read(path).map_err(|e| format!("read failed: {}", e.kind()))?;
    parse_tiff16(&bytes)
}

pub(super) fn parse_tiff16(bytes: &[u8]) -> Result<Tiff16, String> {
    let big = match bytes.get(..2) {
        Some(b"II") => false,
        Some(b"MM") => true,
        _ => return Err("not a TIFF (byte order mark)".into()),
    };
    let u16_at = |at: usize| -> Result<u16, String> {
        let b: [u8; 2] = bytes.get(at..at + 2).ok_or("truncated")?.try_into().unwrap();
        Ok(if big { u16::from_be_bytes(b) } else { u16::from_le_bytes(b) })
    };
    let u32_at = |at: usize| -> Result<u32, String> {
        let b: [u8; 4] = bytes.get(at..at + 4).ok_or("truncated")?.try_into().unwrap();
        Ok(if big { u32::from_be_bytes(b) } else { u32::from_le_bytes(b) })
    };
    match u16_at(2)? {
        42 => {}
        43 => return Err("BigTIFF is not supported".into()),
        _ => return Err("bad TIFF magic".into()),
    }
    let ifd = u32_at(4)? as usize;
    let n = u16_at(ifd)? as usize;
    let mut entries: BTreeMap<u16, Entry> = BTreeMap::new();
    for i in 0..n {
        let at = ifd + 2 + 12 * i;
        let raw: [u8; 4] = bytes.get(at + 8..at + 12).ok_or("truncated")?.try_into().unwrap();
        entries.insert(u16_at(at)?, Entry { ty: u16_at(at + 2)?, count: u32_at(at + 4)?, raw });
    }
    // SHORT(3) / LONG(4) の配列を読む。
    let values = |tag: u16| -> Result<Option<Vec<u32>>, String> {
        let Some(e) = entries.get(&tag) else { return Ok(None) };
        let size = match e.ty {
            3 => 2usize,
            4 => 4usize,
            _ => return Err(format!("tag {tag}: unsupported type {}", e.ty)),
        };
        let total = size * e.count as usize;
        let base = if total <= 4 {
            None
        } else {
            Some(if big { u32::from_be_bytes(e.raw) } else { u32::from_le_bytes(e.raw) } as usize)
        };
        let mut out = Vec::with_capacity(e.count as usize);
        for i in 0..e.count as usize {
            let v = match (base, size) {
                (None, 2) => {
                    let b = [e.raw[2 * i], e.raw[2 * i + 1]];
                    (if big { u16::from_be_bytes(b) } else { u16::from_le_bytes(b) }) as u32
                }
                (None, _) => if big { u32::from_be_bytes(e.raw) } else { u32::from_le_bytes(e.raw) },
                (Some(o), 2) => u16_at(o + 2 * i)? as u32,
                (Some(o), _) => u32_at(o + 4 * i)?,
            };
            out.push(v);
        }
        Ok(Some(out))
    };
    let one = |tag: u16| -> Result<Option<u32>, String> {
        Ok(values(tag)?.and_then(|v| v.first().copied()))
    };
    let need = |tag: u16, name: &str| one(tag)?.ok_or_else(|| format!("missing {name}"));

    let (width, height) = (need(256, "ImageWidth")?, need(257, "ImageLength")?);
    if values(258)?.as_deref() != Some(&[16, 16, 16]) {
        return Err("BitsPerSample is not 16,16,16".into());
    }
    match one(259)? {
        Some(1) | None => {}
        Some(c) => return Err(format!("Compression={c} is not supported (uncompressed only)")),
    }
    if need(262, "PhotometricInterpretation")? != 2 {
        return Err("not RGB photometric".into());
    }
    if need(277, "SamplesPerPixel")? != 3 || entries.contains_key(&338) {
        return Err("not 3 samples/pixel without ExtraSamples (alpha)".into());
    }
    if one(284)?.is_some_and(|p| p != 1) {
        return Err("planar configuration is not chunky".into());
    }
    if one(317)?.is_some_and(|p| p != 1) {
        return Err("Predictor is not supported".into());
    }
    if values(339)?.is_some_and(|f| f.iter().any(|v| *v != 1)) {
        return Err("SampleFormat is not unsigned integer".into());
    }
    if entries.contains_key(&322) || entries.contains_key(&324) {
        return Err("tiled TIFF is not supported".into());
    }
    let offsets = values(273)?.ok_or("missing StripOffsets")?;
    let counts = values(279)?.ok_or("missing StripByteCounts")?;
    if offsets.len() != counts.len() {
        return Err("strip offsets/counts mismatch".into());
    }
    let expected = (width as usize) * (height as usize) * 6;
    let mut data: Vec<u8> = Vec::with_capacity(expected);
    for (o, c) in offsets.iter().zip(&counts) {
        data.extend_from_slice(bytes.get(*o as usize..(*o + *c) as usize).ok_or("strip out of range")?);
    }
    if data.len() != expected {
        return Err(format!("pixel data size {} != expected {expected}", data.len()));
    }
    let samples = data
        .chunks_exact(2)
        .map(|b| if big { u16::from_be_bytes([b[0], b[1]]) } else { u16::from_le_bytes([b[0], b[1]]) })
        .collect();
    Ok(Tiff16 { width, height, samples, big_endian: big })
}

// ---------------------------------------------------------------------------
// 16bit 比較（8bit に落とさない）
// ---------------------------------------------------------------------------

/// |差| の log2 ビン: 0 → "0"、1 → "1"、2..3、4..7、…、32768..65535。
fn bin_of(d: u32) -> usize {
    if d == 0 { 0 } else { (32 - d.leading_zeros()) as usize }
}

fn bin_label(bin: usize) -> String {
    match bin {
        0 => "0".into(),
        1 => "1".into(),
        k => format!("{}-{}", 1u32 << (k - 1), (1u32 << k) - 1),
    }
}

pub(super) fn compare_samples(a: &Tiff16, b: &Tiff16) -> Value {
    if (a.width, a.height) != (b.width, b.height) {
        return json!({"comparable": false, "reason": "dimension mismatch",
            "reference_size": [a.width, a.height], "other_size": [b.width, b.height]});
    }
    let n = a.samples.len() as u64;
    let mut exact = vec![0u64; 65536];
    let mut max_by_channel = [0u32; 3];
    let mut sum_by_channel = [0u64; 3];
    let mut differing_by_channel = [0u64; 3];
    let (mut signed_sum, mut sq_sum, mut differing_pixels) = (0i64, 0u128, 0u64);
    for (pa, pb) in a.samples.chunks_exact(3).zip(b.samples.chunks_exact(3)) {
        let mut pixel_differs = false;
        for c in 0..3 {
            let d = pa[c].abs_diff(pb[c]) as u32;
            exact[d as usize] += 1;
            signed_sum += pb[c] as i64 - pa[c] as i64;
            sq_sum += (d as u128) * (d as u128);
            sum_by_channel[c] += d as u64;
            if d > 0 {
                differing_by_channel[c] += 1;
                pixel_differs = true;
            }
            max_by_channel[c] = max_by_channel[c].max(d);
        }
        differing_pixels += pixel_differs as u64;
    }
    let max = *max_by_channel.iter().max().unwrap();
    let total_diff: u64 = sum_by_channel.iter().sum();
    let differing_samples: u64 = differing_by_channel.iter().sum();
    // 厳密な順位統計（nearest-rank）。
    let percentile = |q: f64| -> u32 {
        let rank = ((q * n as f64).ceil() as u64).clamp(1, n.max(1));
        let mut seen = 0u64;
        for (d, count) in exact.iter().enumerate() {
            seen += count;
            if seen >= rank {
                return d as u32;
            }
        }
        max
    };
    let mut bins = vec![0u64; 17];
    for (d, count) in exact.iter().enumerate() {
        bins[bin_of(d as u32)] += count;
    }
    let histogram: Vec<Value> = bins.iter().enumerate()
        .filter(|(_, c)| **c > 0)
        .map(|(k, c)| json!({"abs_diff": bin_label(k), "samples": c}))
        .collect();
    let per_channel: Vec<Value> = (0..3).map(|c| json!({
        "channel": CHANNELS[c],
        "max_abs_diff": max_by_channel[c],
        "mean_abs_diff": round3(sum_by_channel[c] as f64 / (n / 3).max(1) as f64),
        "differing_samples": differing_by_channel[c],
    })).collect();
    json!({
        "comparable": true,
        "width": a.width, "height": a.height, "samples": n,
        "rawsamples_identical": differing_samples == 0,
        "unit": "16-bit sample value (0..65535); NOT quantized to 8-bit",
        "max_abs_diff": max,
        // 参考: 8bit 換算（/257）。8bit に落として比べたわけではない。
        "max_abs_diff_in_8bit_units": round3(max as f64 / 257.0),
        "mean_abs_diff": round3(total_diff as f64 / n.max(1) as f64),
        "mean_signed_diff_other_minus_reference": round3(signed_sum as f64 / n.max(1) as f64),
        "rms_diff": round3((sq_sum as f64 / n.max(1) as f64).sqrt()),
        "differing_samples": differing_samples,
        "differing_pixels": differing_pixels,
        "pixel_count": (a.width as u64) * (a.height as u64),
        "abs_diff_percentiles": {"p50": percentile(0.5), "p95": percentile(0.95),
            "p99": percentile(0.99), "p999": percentile(0.999)},
        "abs_diff_log2_histogram": histogram,
        "per_channel": per_channel,
    })
}

fn compare_files(reference: &Path, other: &Path) -> Value {
    match (read_tiff16(reference), read_tiff16(other)) {
        (Ok(a), Ok(b)) => {
            let mut v = compare_samples(&a, &b);
            v["bytes_identical"] = json!(fs::read(reference).ok() == fs::read(other).ok());
            v
        }
        (a, b) => json!({"comparable": false, "reason": format!(
            "tiff16 read failed: reference={} other={}",
            a.err().unwrap_or_else(|| "ok".into()), b.err().unwrap_or_else(|| "ok".into()))}),
    }
}

// ---------------------------------------------------------------------------
// 実行
// ---------------------------------------------------------------------------

fn tiff_request(input: &Path, adjustments: &Value, output: &Path) -> Map<String, Value> {
    let mut request = base_request(input, DevelopMode::Preview, Some(adjustments));
    request.insert("format".into(), json!(FORMAT));
    request.insert("output_color_space".into(), json!(OUTPUT_COLOR_SPACE));
    request.insert("output".into(), Value::String(output.to_string_lossy().into_owned()));
    request
}

#[derive(Default)]
struct StepOutputs {
    paths: BTreeMap<usize, PathBuf>,
    reused: BTreeMap<usize, bool>,
    errors: Vec<Value>,
}

fn run_single(cfg: &BenchConfig, input: &Path, series: &Series, dir: &Path, tag: &str) -> StepOutputs {
    let mut out = StepOutputs::default();
    for (step, adjustments) in series.steps.iter().enumerate() {
        let path = dir.join(format!("{tag}-s{step}.tif"));
        let body = Value::Object(tiff_request(input, adjustments, &path)).to_string();
        let result = run_sidecar(&cfg.sidecar, &[], Some(body.as_bytes()), cfg.timeout).and_then(|o| {
            let response = parse_response(&o)?;
            if response.get("ok") != Some(&Value::Bool(true)) || !o.status.success() {
                return Err(cli_error(&response, o.status));
            }
            Ok(())
        });
        match result {
            Ok(()) => { out.paths.insert(step, path); }
            Err(e) => out.errors.push(json!({"method": tag, "step": step, "code": e.code})),
        }
    }
    out
}

fn restart_if_dead(cfg: &BenchConfig, session: &mut Option<batch::BatchSession>, restarts: &mut usize) -> Result<(), CoreImageError> {
    if session.as_ref().is_none_or(|s| !s.is_usable()) {
        if session.is_some() {
            *restarts += 1;
        }
        *session = Some(batch::BatchSession::start(&cfg.sidecar, cfg.timeout)?);
    }
    Ok(())
}

fn run_batch_stateless(cfg: &BenchConfig, input: &Path, series: &Series, dir: &Path, restarts: &mut usize) -> StepOutputs {
    let mut out = StepOutputs::default();
    let mut session = None;
    for (step, adjustments) in series.steps.iter().enumerate() {
        let path = dir.join(format!("batch_stateless-s{step}.tif"));
        let request = Value::Object(tiff_request(input, adjustments, &path));
        let result = restart_if_dead(cfg, &mut session, restarts)
            .and_then(|()| session.as_mut().unwrap().request(&request));
        match result {
            Ok(_) => { out.paths.insert(step, path); }
            Err(e) => out.errors.push(json!({"method": "batch_stateless", "step": step, "code": e.code})),
        }
    }
    out
}

fn run_batch_decode_render(cfg: &BenchConfig, input: &Path, series: &Series, dir: &Path, restarts: &mut usize) -> StepOutputs {
    let mut out = StepOutputs::default();
    let mut session = None;
    let mut held: Option<(String, String)> = None;
    for (step, adjustments) in series.steps.iter().enumerate() {
        let path = dir.join(format!("batch_decode_render-s{step}.tif"));
        let key = decode_key(adjustments);
        let started = *restarts;
        if let Err(e) = restart_if_dead(cfg, &mut session, restarts) {
            out.errors.push(json!({"method": "batch_decode_render", "step": step, "code": e.code}));
            held = None;
            continue;
        }
        if *restarts != started {
            held = None;
        }
        let s = session.as_mut().unwrap();
        let mut reused = false;
        let result: Result<Value, CoreImageError> = (|| {
            if held.as_ref().is_some_and(|(k, _)| *k == key) {
                reused = true;
            } else {
                if let Some((_, old)) = held.take() {
                    s.release(&old)?;
                }
                let mut request = base_request(input, DevelopMode::Preview, Some(adjustments));
                request.remove("format");
                let decoded = s.decode(&Value::Object(request))?;
                held = Some((key.clone(), decoded.id));
            }
            let handle = held.as_ref().map(|(_, h)| h.clone()).expect("held");
            s.render(&handle, &json!({
                "output": path, "format": FORMAT, "output_color_space": OUTPUT_COLOR_SPACE,
                "adjustments": adjustments
            }))
        })();
        match result {
            Ok(_) => {
                out.paths.insert(step, path);
                out.reused.insert(step, reused);
            }
            Err(e) => {
                out.errors.push(json!({"method": "batch_decode_render", "step": step, "code": e.code}));
                if s.is_usable() && batch::is_handle_lost(&e) {
                    held = None;
                } else if !s.is_usable() {
                    held = None;
                }
            }
        }
    }
    out
}

/// 組ごとの要約（撮影 ID・系列をまたいで、最大・平均・異画素数をまとめる）。
fn summarize_pair(comparisons: &[&Value]) -> Value {
    let ok: Vec<&&Value> = comparisons.iter().filter(|c| c["comparable"] == json!(true)).collect();
    let num = |c: &Value, k: &str| c[k].as_f64().unwrap_or(0.0);
    json!({
        "steps_compared": ok.len(),
        "steps_not_comparable": comparisons.len() - ok.len(),
        "all_identical": !ok.is_empty() && ok.iter().all(|c| c["rawsamples_identical"] == json!(true)),
        "max_abs_diff_max": ok.iter().map(|c| num(c, "max_abs_diff")).fold(0.0, f64::max),
        "mean_abs_diff_max": ok.iter().map(|c| num(c, "mean_abs_diff")).fold(0.0, f64::max),
        "mean_abs_diff_mean": round3(if ok.is_empty() { 0.0 } else {
            ok.iter().map(|c| num(c, "mean_abs_diff")).sum::<f64>() / ok.len() as f64 }),
        "differing_pixels_total": ok.iter().map(|c| num(c, "differing_pixels")).sum::<f64>(),
        "differing_samples_total": ok.iter().map(|c| num(c, "differing_samples")).sum::<f64>(),
    })
}

fn definitions() -> Value {
    json!({
        "not_a_quality_threshold": "この差を画質の合否にしない。閾値は未定義。半精度保持(RGBAh)由来の差を示す記録",
        "units": "16bit サンプル値(0..65535)。8bit に量子化せずに比較。max_abs_diff_in_8bit_units は /257 の参考換算のみ",
        "separate_from_jpeg": "JPEG ベンチ(8bit + DCT 量子化 + 色変換)の maxdiff/mean とは別単位。直接比較しない",
        "known_tiff16_difference": "README 記載の既知の差: 640px TIFF16 sRGB で最大 34〜81/65535（decode op が half float で保持）。本対照は 2400px",
        "pairs": {
            "single_repeat": "単発 CLI 2 回目 vs 1 回目。決定性の対照。0 でなければ他の差は解釈できない",
            "batch_stateless": "batch の op 無し現像 vs 単発。輸送だけの対照（同じ経路）",
            "batch_decode_render": "op:decode→op:render vs 単発。RGBAh レイヤ由来の差"
        },
        "request": {"format": FORMAT, "output_color_space": OUTPUT_COLOR_SPACE, "max_edge": PREVIEW_MAX_EDGE,
            "jpeg_quality": "tiff16 に送れないので送らない"},
        "reader": "非圧縮・16bit・RGB・strip の TIFF だけを読む。それ以外は comparable=false と理由を記録（推測しない）",
    })
}

pub(super) fn run_lossless_control(cfg: &BenchConfig, opts: &LosslessOptions) -> Result<Value, CoreImageError> {
    let io = |e: std::io::Error| CoreImageError::new("BENCH_IO", e.to_string());
    let dir = cfg.out_dir.join("lossless");
    fs::create_dir_all(&dir).map_err(io)?;
    let version = cli_version(&cfg.sidecar)?;
    let mut captures = vec![];
    let mut pair_comparisons: BTreeMap<(String, String), Vec<Value>> = BTreeMap::new();
    let mut errors = vec![];
    let mut restarts = 0usize;
    for capture_id in &cfg.capture_ids {
        let input = resolve_arw(&cfg.source_root, capture_id)?;
        let mut series_out = vec![];
        for series in &cfg.series {
            let work = dir.join(format!("{capture_id}-{}", series.name));
            let _ = fs::remove_dir_all(&work);
            fs::create_dir_all(&work).map_err(io)?;
            let a = run_single(cfg, &input, series, &work, "single_a");
            let b = run_single(cfg, &input, series, &work, "single_b");
            let stateless = run_batch_stateless(cfg, &input, series, &work, &mut restarts);
            let decode_render = run_batch_decode_render(cfg, &input, series, &work, &mut restarts);
            for o in [&a, &b, &stateless, &decode_render] {
                for e in &o.errors {
                    let mut e = e.clone();
                    e["capture_id"] = json!(capture_id);
                    e["series"] = json!(series.name);
                    errors.push(e);
                }
            }
            let mut steps_out = vec![];
            for step in 0..series.steps.len() {
                let mut pairs = Map::new();
                for (name, other) in [("single_repeat", &b), ("batch_stateless", &stateless), ("batch_decode_render", &decode_render)] {
                    let comparison = match (a.paths.get(&step), other.paths.get(&step)) {
                        (Some(reference), Some(path)) => compare_files(reference, path),
                        _ => json!({"comparable": false, "reason": "a request failed (see errors)"}),
                    };
                    pair_comparisons
                        .entry((name.to_string(), series.name.clone()))
                        .or_default()
                        .push(comparison.clone());
                    let mut comparison = comparison;
                    if name == "batch_decode_render" {
                        comparison["decode_reused"] = json!(other.reused.get(&step));
                    }
                    pairs.insert(name.to_string(), comparison);
                }
                steps_out.push(json!({"step": step, "pairs": Value::Object(pairs)}));
            }
            if !opts.keep_images {
                let _ = fs::remove_dir_all(&work);
            }
            series_out.push(json!({"series": series.name, "class": series.class.as_str(), "steps": steps_out}));
        }
        captures.push(json!({"capture_id": capture_id, "series": series_out}));
    }
    let summary: Vec<Value> = pair_comparisons.iter().map(|((pair, series), v)| {
        let refs: Vec<&Value> = v.iter().collect();
        let mut s = summarize_pair(&refs);
        s["pair"] = json!(pair);
        s["series"] = json!(series);
        s
    }).collect();
    Ok(json!({
        "schema": "geoframe.lossless-control.v1",
        "generated_at_unix": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0),
        "build_profile": if cfg!(debug_assertions) { "debug" } else { "release" },
        "cli": {"cli_version": version.cli_version, "macos_version": version.macos_version,
                "binary_sha256_12": version.binary_sha256.chars().take(12).collect::<String>()},
        "environment": {
            "storage": filesystem_evidence(&cfg.source_root, &cfg.storage_label),
            "timeout_s": cfg.timeout.as_secs_f64(),
            "base_adjustments": base_adjustments(),
            "session_restarts": restarts,
        },
        "definitions": definitions(),
        "errors": errors,
        "summary_by_pair_and_series": summary,
        "captures": captures,
    }))
}

// ---------------------------------------------------------------------------
// テスト
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    struct Dir(PathBuf);
    impl Dir {
        fn new(tag: &str) -> Self {
            static N: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "geoframe-bench141-ll-{tag}-{}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> &Path { &self.0 }
    }
    impl Drop for Dir {
        fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
    }

    const W: u32 = 12;
    const H: u32 = 10;

    /// 実 CLI の出力に合わせた TIFF16 を書く（既定: ビッグエンディアン・非圧縮・9 行/strip・SampleFormat あり）。
    fn tiff_bytes(big: bool, rows_per_strip: u32, compression: u16, spp: u16, f: &dyn Fn(u32, u32, usize) -> u16) -> Vec<u8> {
        let w16 = |v: u16| if big { v.to_be_bytes().to_vec() } else { v.to_le_bytes().to_vec() };
        let w32 = |v: u32| if big { v.to_be_bytes().to_vec() } else { v.to_le_bytes().to_vec() };
        let mut pixels: Vec<u8> = vec![];
        for y in 0..H { for x in 0..W { for c in 0..spp as usize { pixels.extend(w16(f(x, y, c))); } } }
        let row_bytes = (W as usize) * spp as usize * 2;
        let strips: Vec<(usize, usize)> = (0..H.div_ceil(rows_per_strip)).map(|s| {
            let first = (s * rows_per_strip) as usize;
            let rows = (rows_per_strip as usize).min(H as usize - first);
            (first * row_bytes, rows * row_bytes)
        }).collect();
        // レイアウト: ヘッダ 8 | ピクセル | 外部値（bits, offsets, counts, sampleformat）| IFD
        let mut file = vec![if big { b'M' } else { b'I' }; 2];
        file.extend(w16(42));
        let ifd_pos_at = file.len();
        file.extend(w32(0));
        let pixel_base = file.len();
        file.extend(&pixels);
        let bits_at = file.len();
        for _ in 0..spp { file.extend(w16(16)); }
        let offs_at = file.len();
        for (o, _) in &strips { file.extend(w32((pixel_base + o) as u32)); }
        let counts_at = file.len();
        for (_, c) in &strips { file.extend(w32(*c as u32)); }
        // 1 strip のときは値が 4byte に収まるので、外部配列ではなく IFD に直接入れる。
        let (offs_value, counts_value) = if strips.len() == 1 {
            (w32((pixel_base + strips[0].0) as u32), w32(strips[0].1 as u32))
        } else {
            (w32(offs_at as u32), w32(counts_at as u32))
        };
        let sf_at = file.len();
        for _ in 0..spp { file.extend(w16(1)); }
        let ifd_at = file.len();
        let mut entries: Vec<(u16, u16, u32, Vec<u8>)> = vec![
            (256, 3, 1, { let mut v = w16(W as u16); v.extend([0, 0]); v }),
            (257, 3, 1, { let mut v = w16(H as u16); v.extend([0, 0]); v }),
            (258, 3, spp as u32, w32(bits_at as u32)),
            (259, 3, 1, { let mut v = w16(compression); v.extend([0, 0]); v }),
            (262, 3, 1, { let mut v = w16(2); v.extend([0, 0]); v }),
            (273, 4, strips.len() as u32, offs_value),
            (277, 3, 1, { let mut v = w16(spp); v.extend([0, 0]); v }),
            (278, 3, 1, { let mut v = w16(rows_per_strip as u16); v.extend([0, 0]); v }),
            (279, 4, strips.len() as u32, counts_value),
            (339, 3, spp as u32, w32(sf_at as u32)),
        ];
        entries.sort_by_key(|e| e.0);
        file.extend(w16(entries.len() as u16));
        for (tag, ty, count, value) in entries {
            file.extend(w16(tag)); file.extend(w16(ty)); file.extend(w32(count)); file.extend(value);
        }
        file.extend(w32(0));
        let at = w32(ifd_at as u32);
        file[ifd_pos_at..ifd_pos_at + 4].copy_from_slice(&at);
        file
    }

    fn base_pixel(x: u32, y: u32, c: usize) -> u16 {
        (((x * 3000 + y * 700 + c as u32 * 4000) % 60000) + 257 * 10) as u16
    }

    fn write_tiff(path: &Path, f: &dyn Fn(u32, u32, usize) -> u16) {
        fs::write(path, tiff_bytes(true, 4, 1, 3, f)).unwrap();
    }

    #[test]
    fn reader_roundtrips_both_byte_orders_and_multiple_strips() {
        for (big, rows) in [(true, 9), (true, 4), (false, 3), (false, 10)] {
            let bytes = tiff_bytes(big, rows, 1, 3, &base_pixel);
            let t = parse_tiff16(&bytes).unwrap();
            assert_eq!((t.width, t.height, t.big_endian), (W, H, big));
            for y in 0..H { for x in 0..W { for c in 0..3 {
                assert_eq!(t.samples[((y * W + x) * 3) as usize + c], base_pixel(x, y, c), "{big} {rows}");
            } } }
        }
    }

    #[test]
    fn reader_rejects_layouts_it_cannot_read_exactly() {
        let lzw = tiff_bytes(true, 4, 5, 3, &base_pixel);
        assert!(parse_tiff16(&lzw).unwrap_err().contains("Compression=5"));
        let rgba = tiff_bytes(true, 4, 1, 4, &base_pixel);
        assert!(parse_tiff16(&rgba).is_err());
        let mut bigtiff = tiff_bytes(true, 4, 1, 3, &base_pixel);
        bigtiff[3] = 43;
        assert!(parse_tiff16(&bigtiff).unwrap_err().contains("BigTIFF"));
        assert!(parse_tiff16(b"not a tiff").is_err());
        let truncated = tiff_bytes(true, 4, 1, 3, &base_pixel);
        assert!(parse_tiff16(&truncated[..truncated.len() / 2]).is_err());
    }

    #[test]
    fn sixteen_bit_compare_sees_differences_an_8bit_comparison_would_hide() {
        // 257 = 8bit の 1 階調。+100 は 8bit に落とすと同じ値になる。
        let a = parse_tiff16(&tiff_bytes(true, 4, 1, 3, &|_, _, _| 257 * 10)).unwrap();
        let b = parse_tiff16(&tiff_bytes(true, 4, 1, 3, &|x, y, c| {
            if (x, y, c) == (3, 4, 1) { 257 * 10 + 100 } else { 257 * 10 }
        })).unwrap();
        assert!(a.samples.iter().zip(&b.samples).all(|(p, q)| p >> 8 == q >> 8), "8bit では区別できない");
        let v = compare_samples(&a, &b);
        assert_eq!(v["rawsamples_identical"], json!(false));
        assert_eq!(v["max_abs_diff"], json!(100));
        assert_eq!(v["differing_samples"], json!(1));
        assert_eq!(v["differing_pixels"], json!(1));
        assert_eq!(v["per_channel"][1]["max_abs_diff"], json!(100));
        assert_eq!(v["per_channel"][0]["max_abs_diff"], json!(0));
        assert_eq!(v["abs_diff_percentiles"]["p50"], json!(0));
        // 360 サンプル中 1 つだけ非 0: nearest-rank の p99 は 0、p99.9 は最大値になる（n が小さいと極値に張り付く）。
        assert_eq!(v["abs_diff_percentiles"]["p99"], json!(0));
        assert_eq!(v["abs_diff_percentiles"]["p999"], json!(100));
        // 100 は 64..127 のビン
        assert_eq!(v["abs_diff_log2_histogram"], json!([
            {"abs_diff": "0", "samples": (W * H * 3 - 1) as u64}, {"abs_diff": "64-127", "samples": 1}]));
        assert!((v["max_abs_diff_in_8bit_units"].as_f64().unwrap() - 0.389).abs() < 1e-9);
        assert_eq!(v["mean_signed_diff_other_minus_reference"].as_f64().unwrap() > 0.0, true);
    }

    #[test]
    fn compare_reports_identical_and_dimension_mismatch() {
        let a = parse_tiff16(&tiff_bytes(true, 4, 1, 3, &base_pixel)).unwrap();
        let same = compare_samples(&a, &parse_tiff16(&tiff_bytes(false, 9, 1, 3, &base_pixel)).unwrap());
        assert_eq!(same["rawsamples_identical"], json!(true));
        assert_eq!(same["max_abs_diff"], json!(0));
        assert_eq!(same["abs_diff_log2_histogram"], json!([{"abs_diff": "0", "samples": (W * H * 3) as u64}]));
        let other = Tiff16 { width: W + 1, height: H, samples: vec![], big_endian: true };
        assert_eq!(compare_samples(&a, &other)["comparable"], json!(false));
    }

    #[test]
    fn histogram_bins_and_percentiles_for_a_known_distribution() {
        // 差: 0(大半) / 1 / 3 / 8 / 40000 を 1 サンプルずつ
        let diffs = [(0u32, 1u16), (1, 1), (2, 3), (3, 8), (4, 0)];
        let a = parse_tiff16(&tiff_bytes(true, 4, 1, 3, &|_, _, _| 1000)).unwrap();
        let b = parse_tiff16(&tiff_bytes(true, 4, 1, 3, &|x, y, c| {
            if y == 0 && c == 0 && x < 4 { 1000 + diffs[x as usize].1 } else if (x, y, c) == (11, 9, 2) { 1000 + 40000 } else { 1000 }
        })).unwrap();
        let v = compare_samples(&a, &b);
        assert_eq!(v["max_abs_diff"], json!(40000));
        let hist = v["abs_diff_log2_histogram"].as_array().unwrap();
        let get = |label: &str| hist.iter().find(|h| h["abs_diff"] == json!(label)).map(|h| h["samples"].as_u64().unwrap());
        assert_eq!(get("1"), Some(2)); // 1, 1
        assert_eq!(get("2-3"), Some(1)); // 3
        assert_eq!(get("8-15"), Some(1)); // 8
        assert_eq!(get("32768-65535"), Some(1)); // 40000
        assert_eq!(v["differing_samples"], json!(5));
    }

    /// 本物の CLI の代わりをする sh スクリプト。tiff16 の契約を検査する（`jpeg_quality` 同梱・format/色空間欠落は拒否）。
    /// `render` op は `alt` の TIFF、それ以外は `base` の TIFF を出力パスへコピーする。
    fn fake_cli(dir: &Path, base: &Path, alt: &Path) -> PathBuf {
        let script = format!(r##"#!/bin/sh
BASE='{base}'
ALT='{alt}'
ok() {{
  printf '{{"ok":true,"cli_version":"0.8.0","macos_version":"27.0.1","decoder_version":"9","mode":"preview","format":"tiff16","output":"%s","width":12,"height":10,"elapsed_ms":{{"decode":1.0,"render_encode":2.0,"total":3.0}},"applied":{{"lens_correction_enabled":true}},"source":{{}}%s}}\n' "$1" "$2"
}}
bad() {{ printf '{{"error":{{"code":"INVALID_REQUEST","message":"%s"}},"ok":false%s}}\n' "$1" "$2"; }}
check_tiff() {{
  case "$1" in *'"format":"tiff16"'*) ;; *) return 1 ;; esac
  case "$1" in *'"output_color_space":"srgb"'*) ;; *) return 1 ;; esac
  case "$1" in *jpeg_quality*) return 1 ;; esac
  return 0
}}
if [ "$1" = "--version" ]; then
  printf '%s\n' '{{"cli_version":"0.8.0","engine":"coreimage","macos_version":"27.0.1","ok":true}}'
  exit 0
fi
if [ "$1" = "--batch" ]; then
  n=0
  while IFS= read -r line; do
    n=$((n+1))
    out=$(printf '%s' "$line" | sed -n 's/.*"output":"\([^"]*\)".*/\1/p')
    case "$line" in
      *'"exposure":1.5,'*) bad boom ",\"sequence\":$n" ;;
      *'"op":"decode"'*)
        case "$line" in *'"format"'*|*'"output"'*) bad "decode takes no format/output" ",\"sequence\":$n" ;;
          *) printf '{{"ok":true,"op":"decode","handle":"h%d","width":12,"height":10,"bytes":10,"elapsed_ms":{{"decode":1.0,"render_encode":0.5,"total":1.5}},"sequence":%d}}\n' "$n" "$n" ;; esac ;;
      *'"op":"release"'*) printf '{{"ok":true,"op":"release","released":true,"sequence":%d}}\n' "$n" ;;
      *'"op":"render"'*)
        if check_tiff "$line"; then cp "$ALT" "$out"; ok "$out" ",\"op\":\"render\",\"sequence\":$n"; else bad "tiff contract" ",\"sequence\":$n"; fi ;;
      *) if check_tiff "$line"; then cp "$BASE" "$out"; ok "$out" ",\"sequence\":$n"; else bad "tiff contract" ",\"sequence\":$n"; fi ;;
    esac
  done
  exit 0
fi
req=$(cat)
out=$(printf '%s' "$req" | sed -n 's/.*"output":"\([^"]*\)".*/\1/p')
case "$req" in *'"exposure":1.5,'*) bad boom ""; exit 1 ;; esac
if check_tiff "$req"; then cp "$BASE" "$out"; ok "$out" ""; else bad "tiff contract" ""; exit 1; fi
"##, base = base.display(), alt = alt.display());
        let path = dir.join("fake-cli");
        fs::write(&path, script).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn setup(tag: &str) -> (Dir, BenchConfig) {
        let dir = Dir::new(tag);
        let (base, alt) = (dir.path().join("base.tif"), dir.path().join("alt.tif"));
        write_tiff(&base, &base_pixel);
        // alt: 一部のサンプルに小さな差（1 と 100）。8bit では見えない大きさ。
        write_tiff(&alt, &|x, y, c| {
            let v = base_pixel(x, y, c);
            match (x, y, c) { (1, 1, 0) => v + 1, (2, 2, 1) => v + 100, _ => v }
        });
        let source = dir.path().join("src");
        fs::create_dir_all(&source).unwrap();
        fs::write(source.join("DSC00001.ARW"), b"fake raw").unwrap();
        fs::write(source.join("DSC00002.ARW"), b"fake raw").unwrap();
        let cfg = BenchConfig {
            sidecar: fake_cli(dir.path(), &base, &alt),
            source_root: source,
            capture_ids: vec!["DSC00001".into(), "DSC00002".into()],
            out_dir: dir.path().join("out"),
            storage_label: "local-test".into(),
            warm_reps: 0,
            timeout: Duration::from_secs(2),
            series: vec![
                numeric_series("t_decode", SeriesClass::DecodeChanging, "exposure", &[0.0, 0.5]),
                numeric_series("t_post", SeriesClass::PostOnly, "shadow_lift", &[10.0, 20.0, 30.0]),
            ],
            methods: vec![],
            rss_probe: false,
        };
        (dir, cfg)
    }

    fn pair<'a>(report: &'a Value, cap: usize, series: &str, step: usize, name: &str) -> &'a Value {
        let s = report["captures"][cap]["series"].as_array().unwrap().iter()
            .find(|s| s["series"] == json!(series)).unwrap();
        &s["steps"][step]["pairs"][name]
    }

    #[test]
    fn fake_cli_lossless_control_contract_and_pairs() {
        let (_dir, cfg) = setup("contract");
        let report = run_lossless_control(&cfg, &LosslessOptions { keep_images: false }).unwrap();
        assert_eq!(report["schema"], json!("geoframe.lossless-control.v1"));
        assert_eq!(report["errors"], json!([]), "tiff16 契約違反の要求があれば fake が INVALID_REQUEST を返す");
        assert_eq!(report["captures"].as_array().unwrap().len(), 2);
        for cap in 0..2 {
            for (series, steps) in [("t_decode", 2), ("t_post", 3)] {
                for step in 0..steps {
                    for name in ["single_repeat", "batch_stateless"] {
                        let p = pair(&report, cap, series, step, name);
                        assert_eq!(p["rawsamples_identical"], json!(true), "{name}");
                        assert_eq!(p["bytes_identical"], json!(true));
                    }
                    let d = pair(&report, cap, series, step, "batch_decode_render");
                    assert_eq!(d["rawsamples_identical"], json!(false));
                    assert_eq!(d["max_abs_diff"], json!(100));
                    assert_eq!(d["differing_samples"], json!(2));
                    assert_eq!(d["differing_pixels"], json!(2));
                    assert_eq!(d["unit"].as_str().unwrap().contains("NOT quantized"), true);
                }
            }
            // decode 系列は毎ステップ decode、後段系列は先頭だけ decode
            assert_eq!(pair(&report, cap, "t_decode", 1, "batch_decode_render")["decode_reused"], json!(false));
            assert_eq!(pair(&report, cap, "t_post", 0, "batch_decode_render")["decode_reused"], json!(false));
            assert_eq!(pair(&report, cap, "t_post", 2, "batch_decode_render")["decode_reused"], json!(true));
        }
        // 要約: 組 × 系列
        let summary = report["summary_by_pair_and_series"].as_array().unwrap();
        assert_eq!(summary.len(), 3 * 2);
        let dr = summary.iter().find(|s| s["pair"] == json!("batch_decode_render") && s["series"] == json!("t_post")).unwrap();
        assert_eq!(dr["steps_compared"], json!(6)); // 2 captures × 3 steps
        assert_eq!(dr["all_identical"], json!(false));
        assert_eq!(dr["max_abs_diff_max"], json!(100.0));
        let sr = summary.iter().find(|s| s["pair"] == json!("single_repeat") && s["series"] == json!("t_decode")).unwrap();
        assert_eq!(sr["all_identical"], json!(true));
        // 公開 JSON に一時ディレクトリのパスや画素列を出さない。定義は JPEG と別単位・閾値未定義を明記。
        let text = report.to_string();
        assert!(!text.contains(&std::env::temp_dir().to_string_lossy().into_owned()), "path leaked");
        assert!(report["definitions"]["not_a_quality_threshold"].as_str().unwrap().contains("閾値は未定義"));
        assert!(report["definitions"]["separate_from_jpeg"].as_str().unwrap().contains("別単位"));
        // 既定では TIFF を残さない
        assert!(!cfg.out_dir.join("lossless/DSC00001-t_post").exists());
    }

    #[test]
    fn fake_cli_lossless_keep_images_and_failed_request_do_not_abort() {
        let (_dir, mut cfg) = setup("fail");
        cfg.capture_ids.truncate(1);
        cfg.series = vec![numeric_series("t_err", SeriesClass::DecodeChanging, "exposure", &[0.0, 1.5, -0.5])];
        let report = run_lossless_control(&cfg, &LosslessOptions { keep_images: true }).unwrap();
        // 1.5 のステップは 4 経路すべてで失敗し、他のステップは比較される。
        let codes: Vec<(&str, u64)> = report["errors"].as_array().unwrap().iter()
            .map(|e| (e["method"].as_str().unwrap(), e["step"].as_u64().unwrap())).collect();
        assert_eq!(codes.len(), 4);
        assert!(codes.iter().all(|(_, step)| *step == 1));
        assert_eq!(pair(&report, 0, "t_err", 1, "batch_stateless")["comparable"], json!(false));
        assert_eq!(pair(&report, 0, "t_err", 2, "batch_stateless")["rawsamples_identical"], json!(true));
        assert!(cfg.out_dir.join("lossless/DSC00001-t_err/single_a-s0.tif").exists());
    }

    /// 実 CLI が出した TIFF16 を読めるか（親が任意で実行。RAW は使わず既存 TIFF を読むだけ）。
    #[test]
    #[ignore]
    fn tiff16_reader_reads_real_cli_output() {
        let path = std::env::var("GEOFRAME_TRANSPORT_BENCH_TIFF_SAMPLE").expect("GEOFRAME_TRANSPORT_BENCH_TIFF_SAMPLE");
        let t = read_tiff16(Path::new(&path)).expect("readable");
        println!("{}x{} big_endian={}", t.width, t.height, t.big_endian);
        assert_eq!(t.samples.len(), (t.width * t.height * 3) as usize);
        assert!(compare_samples(&t, &t)["rawsamples_identical"] == json!(true));
    }

    /// 実素材の可逆対照（親が実行）。手順: docs/reports/coreimage-preview-transport-bench.md §10
    #[test]
    #[ignore]
    fn coreimage_lossless_control() {
        assert!(!cfg!(debug_assertions), "--release で実行してください");
        let env = |name: &str| std::env::var(format!("GEOFRAME_TRANSPORT_BENCH_{name}")).ok().filter(|v| !v.trim().is_empty());
        let need = |name: &str| env(name).unwrap_or_else(|| panic!("GEOFRAME_TRANSPORT_BENCH_{name} を指定してください"));
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let sidecar = env("SIDECAR").map(PathBuf::from)
            .unwrap_or_else(|| repo.join("src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin"));
        assert!(fs::metadata(&sidecar).map(|m| m.len()).unwrap_or(0) > 100_000,
            "{} は実 CLI ではありません（スタブ?）", sidecar.display());
        let capture_ids: Vec<String> = need("CAPTURES").split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
        assert!(!capture_ids.is_empty() && capture_ids.iter().all(|c| is_valid_capture_id(c)), "CAPTURES は DSC00000 形式をカンマ区切り");
        // 既定の系列は追加対照の 2 系列（decode 段 1 + 後段 1）。SERIES で上書き可。
        let wanted: Vec<String> = env("SERIES").map(|v| v.split(',').map(|s| s.trim().to_string()).collect())
            .unwrap_or_else(|| vec!["decode_exposure".into(), "post_shadow_lift".into()]);
        let mut series = standard_series();
        series.retain(|s| wanted.contains(&s.name));
        assert!(!series.is_empty(), "SERIES に一致する系列がありません");
        let out_dir = PathBuf::from(need("OUT"));
        let storage_label = need("STORAGE_LABEL");
        let cfg = BenchConfig {
            sidecar,
            source_root: PathBuf::from(need("SOURCE_ROOT")).canonicalize().expect("SOURCE_ROOT"),
            capture_ids, out_dir: out_dir.clone(), storage_label: storage_label.clone(),
            warm_reps: 0,
            timeout: Duration::from_secs(env("TIMEOUT_S").map_or(60, |v| v.parse().expect("TIMEOUT_S"))),
            series, methods: vec![], rss_probe: false,
        };
        let opts = LosslessOptions { keep_images: env("KEEP_IMAGES").is_some_and(|v| v == "1") };
        let report = run_lossless_control(&cfg, &opts).expect("lossless control");
        let unix = report["generated_at_unix"].as_u64().unwrap_or(0);
        let label: String = storage_label.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
        let path = out_dir.join(format!("lossless-control-{label}-{unix}.json"));
        fs::write(&path, serde_json::to_string_pretty(&report).unwrap() + "\n").unwrap();
        println!("結果: {}", path.display());
    }
}
