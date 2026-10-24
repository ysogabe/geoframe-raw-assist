//! #141 Stage0: 手動プレビュー（2400px JPEG）の輸送方式比較ベンチ（test 専用）。
//!
//! 本番の現像・キャッシュ・候補には触れない。同じ ARW・同じ調整列・同じ出力寸法/品質で、次の方式を測る。
//!
//! | method | 内容 | 測定ラベル |
//! |---|---|---|
//! | `single_cli` | 既存 `run_sidecar` で単発 CLI（本番と同じ起動・待ち方）。リクエスト JSON は下の 2 つと同一 | 輸送比較 |
//! | `rust_develop_miss` | 既存 `coreimage::develop()`（解決・キャッシュ鍵・CLI・来歴書き込み・prune）。反復ごとに空の app_data | 輸送比較（アプリ全体） |
//! | `rust_develop_cache_hit` | 同じ app_data で再度 `develop()`。**CLI を呼ばない完成 JPEG のキャッシュヒット** | **輸送比較ではない**（別集計） |
//! | `batch_stateless` | 既存 `BatchSession` に `op` 無しの現像行を流す（毎回デコード） | 輸送比較 |
//! | `batch_decode_render` | `op: decode` → `op: render`。後段だけの系列ではハンドルを再利用 | 輸送比較 |
//!
//! 素材・出力先は環境変数だけで指定し、勝手に列挙・現像しない（`GEOFRAME_TRANSPORT_BENCH_*`。`docs/reports/` の手順書）。
//! 結果は JSON 1 ファイル。画像・EXIF・GPS・私的な絶対パスは JSON に書かない。
//! 実素材を使う `coreimage_transport_bench` は `#[ignore]`。fake CLI の契約テストは通常のテストで走る。
//!
//! 測定の限界（JSON の `definitions` にも同じ文を入れる）:
//! - wall は Rust プロセス内の `Instant`。React / Tauri IPC / WebView 表示は含まない（Stage1 の UI フックで別に測る）。
//! - cold = bench プロセス内の「そのセッション/系列の最初の反復」。OS のファイルキャッシュ・GPU 状態の cold は保証しない。
//! - 画質の同等性は maxdiff を記録するだけ。閾値は未決で、ここでは合否にしない。
use super::*;
use serde_json::Map;
use std::collections::BTreeMap;

// 可逆(TIFF16)出力での対照。JPEG ベンチとは独立（lossless.rs）。
mod lossless;

// ---------------------------------------------------------------------------
// 調整列
// ---------------------------------------------------------------------------

/// 系列の種別。`decode_changing` はデコード段のキーが動く（毎回デコードが必要）。
/// `post_only` は後段だけが動く（デコード結果を再利用できる）。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum SeriesClass {
    DecodeChanging,
    PostOnly,
}

impl SeriesClass {
    fn as_str(self) -> &'static str {
        match self {
            SeriesClass::DecodeChanging => "decode_changing",
            SeriesClass::PostOnly => "post_only",
        }
    }
}

#[derive(Clone, Debug)]
pub(super) struct Series {
    pub(super) name: String,
    pub(super) class: SeriesClass,
    /// `normalize_adjustments` 済みの 9 キー全量。
    pub(super) steps: Vec<Value>,
}

/// アプリの一般的な初期値に近い基準（プリセットの値ではない。ベンチ内で固定）。
pub(super) fn base_adjustments() -> Value {
    json!({
        "exposure": 0.0, "wb_temp": 0.0, "shadow_lift": 15.0, "highlight_recovery": 60.0,
        "brightness": 1.0, "contrast": 1.03, "saturation": 1.02, "sharpness": 1.04,
        "noise_reduction": "light"
    })
}

fn with(key: &str, value: Value) -> Value {
    let mut adjustments = base_adjustments();
    adjustments[key] = value;
    crate::adjustments::normalize_adjustments(&adjustments).expect("bench adjustments are valid")
}

fn numeric_series(name: &str, class: SeriesClass, key: &str, values: &[f64]) -> Series {
    Series {
        name: name.to_string(),
        class,
        steps: values.iter().map(|v| with(key, json!(v))).collect(),
    }
}

/// 固定の調整列。decode 段を動かす 5 系列と、後段だけを動かす 5 系列。
pub(super) fn standard_series() -> Vec<Series> {
    use SeriesClass::{DecodeChanging as D, PostOnly as P};
    vec![
        numeric_series("decode_exposure", D, "exposure", &[-0.4, -0.2, 0.0, 0.2, 0.4]),
        numeric_series("decode_highlight_recovery", D, "highlight_recovery", &[0.0, 30.0, 60.0, 90.0, 100.0]),
        numeric_series("decode_wb_temp", D, "wb_temp", &[-20.0, -10.0, 0.0, 10.0, 20.0]),
        // デコード段に効くのは min(sharpness, 1)。1 以下で動かす。
        numeric_series("decode_min_sharpness", D, "sharpness", &[0.8, 0.85, 0.9, 0.95, 1.0]),
        Series {
            name: "decode_noise_reduction".into(),
            class: D,
            steps: ["off", "light", "full"].iter().map(|v| with("noise_reduction", json!(v))).collect(),
        },
        numeric_series("post_shadow_lift", P, "shadow_lift", &[0.0, 10.0, 20.0, 30.0, 40.0]),
        numeric_series("post_brightness", P, "brightness", &[0.95, 0.975, 1.0, 1.025, 1.05]),
        numeric_series("post_contrast", P, "contrast", &[0.95, 1.0, 1.03, 1.06, 1.1]),
        numeric_series("post_saturation", P, "saturation", &[0.9, 0.95, 1.02, 1.05, 1.1]),
        // sharpness > 1 の増分だけが後段。
        numeric_series("post_sharpness_gt1", P, "sharpness", &[1.05, 1.15, 1.25, 1.35, 1.45]),
    ]
}

/// `op: render` が decode 時と一致を要求するキー（`Renderer::key` と同じ組）。
pub(super) fn decode_key(adjustments: &Value) -> String {
    json!([
        adjustments["exposure"],
        adjustments["highlight_recovery"],
        adjustments["wb_temp"],
        adjustments["sharpness"].as_f64().unwrap_or(1.0).min(1.0),
        adjustments["noise_reduction"]
    ])
    .to_string()
}

// ---------------------------------------------------------------------------
// 統計・解析の純関数
// ---------------------------------------------------------------------------

fn round3(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

/// 最近順位法（nearest-rank）の百分位。n が小さいと p95 は実質 max になる。
pub(super) fn percentile(sorted: &[f64], q: f64) -> Option<f64> {
    if sorted.is_empty() {
        return None;
    }
    let rank = ((q * sorted.len() as f64).ceil() as usize).clamp(1, sorted.len());
    Some(sorted[rank - 1])
}

/// n / min / p50 / p95 / max / mean。`small_sample` は n < 20（p95 を率として読まないための印）。
pub(super) fn stats(values: &[f64]) -> Value {
    let mut sorted: Vec<f64> = values.iter().copied().filter(|v| v.is_finite()).collect();
    sorted.sort_by(f64::total_cmp);
    if sorted.is_empty() {
        return json!({"n": 0});
    }
    let mean = sorted.iter().sum::<f64>() / sorted.len() as f64;
    json!({
        "n": sorted.len(),
        "min": round3(sorted[0]),
        "p50": round3(percentile(&sorted, 0.5).unwrap()),
        "p95": round3(percentile(&sorted, 0.95).unwrap()),
        "max": round3(*sorted.last().unwrap()),
        "mean": round3(mean),
        "small_sample": sorted.len() < 20,
    })
}

/// `GEOFRAME_CI_TIMING=1` の stderr 1 行（`geoframe-ci-develop: timing create=… total=…`）を読む。
pub(super) fn parse_ci_timing(stderr: &str) -> Option<Value> {
    let line = stderr.lines().rev().find(|l| l.contains(": timing "))?;
    let body = line.split(": timing ").nth(1)?;
    let mut map = Map::new();
    for token in body.split_whitespace() {
        let (key, value) = token.split_once('=')?;
        map.insert(key.to_string(), json!(value.parse::<f64>().ok()?));
    }
    (!map.is_empty()).then_some(Value::Object(map))
}

/// `mount` の出力から、`path` を含む最長一致のマウントの fstype を返す。
/// 返すのは fstype とルート(`/`)かどうかだけ（デバイス名には user@host が入るので JSON へ出さない）。
pub(super) fn mount_fstype(mount_output: &str, path: &Path) -> Option<(String, bool)> {
    let mut best: Option<(usize, String, bool)> = None;
    for line in mount_output.lines() {
        let Some((_, rest)) = line.split_once(" on ") else { continue };
        let Some(open) = rest.rfind(" (") else { continue };
        let mount_point = Path::new(&rest[..open]);
        let options = rest[open + 2..].trim_end_matches(')');
        let fstype = options.split(',').next().unwrap_or("").trim();
        if fstype.is_empty() || !path.starts_with(mount_point) {
            continue;
        }
        let depth = mount_point.components().count();
        if best.as_ref().is_none_or(|(d, _, _)| depth > *d) {
            best = Some((depth, fstype.to_string(), mount_point == Path::new("/")));
        }
    }
    best.map(|(_, fstype, root)| (fstype, root))
}

pub(super) fn is_network_fstype(fstype: &str) -> bool {
    matches!(fstype, "smbfs" | "nfs" | "afpfs" | "webdav" | "cifs")
}

/// `/usr/bin/time -l` の stderr から最大 RSS（byte）と peak memory footprint（byte）を読む。
pub(super) fn parse_time_l(stderr: &str) -> Value {
    let find = |needle: &str| {
        stderr.lines().find_map(|l| {
            let l = l.trim();
            l.ends_with(needle)
                .then(|| l.split_whitespace().next()?.parse::<u64>().ok())
                .flatten()
        })
    };
    json!({
        "max_resident_set_bytes": find("maximum resident set size"),
        "peak_memory_footprint_bytes": find("peak memory footprint"),
    })
}

/// 2 枚の JPEG をデコードして RGB を比較する。閾値は持たない（記録のみ）。
pub(super) fn compare_rgb(reference: &Path, other: &Path) -> Value {
    let read = |p: &Path| image::open(p).map(|i| i.to_rgb8());
    let (a, b) = match (read(reference), read(other)) {
        (Ok(a), Ok(b)) => (a, b),
        (a, b) => {
            return json!({"comparable": false, "reason": format!(
                "decode failed: reference={} other={}", a.is_ok(), b.is_ok())});
        }
    };
    if a.dimensions() != b.dimensions() {
        return json!({"comparable": false, "reason": "dimension mismatch",
            "reference_size": [a.width(), a.height()], "other_size": [b.width(), b.height()]});
    }
    let (mut max, mut sum, mut differing) = (0u8, 0u64, 0u64);
    for (pa, pb) in a.pixels().zip(b.pixels()) {
        let d = pa.0.iter().zip(pb.0.iter()).map(|(x, y)| x.abs_diff(*y)).max().unwrap_or(0);
        max = max.max(d);
        sum += pa.0.iter().zip(pb.0.iter()).map(|(x, y)| x.abs_diff(*y) as u64).sum::<u64>();
        differing += (d > 0) as u64;
    }
    let pixels = (a.width() as u64) * (a.height() as u64);
    let bytes_identical = fs::read(reference).ok() == fs::read(other).ok();
    json!({
        "comparable": true,
        "width": a.width(), "height": a.height(),
        "rawpixels_identical": differing == 0,
        "bytes_identical": bytes_identical,
        "max_abs_diff": max,
        "mean_abs_diff": round3(sum as f64 / (pixels * 3).max(1) as f64),
        "differing_pixels": differing, "pixel_count": pixels,
    })
}

fn sha256_hex12(path: &Path) -> Option<String> {
    let bytes = fs::read(path).ok()?;
    Some(Sha256::digest(&bytes).iter().take(6).map(|b| format!("{b:02x}")).collect())
}

// ---------------------------------------------------------------------------
// 設定
// ---------------------------------------------------------------------------

pub(super) struct BenchConfig {
    pub(super) sidecar: PathBuf,
    pub(super) source_root: PathBuf,
    pub(super) capture_ids: Vec<String>,
    pub(super) out_dir: PathBuf,
    /// ユーザーが付ける保管場所ラベル（"local" / "smb" など）。必須。
    pub(super) storage_label: String,
    /// cold 1 回に加える warm 反復の数。
    pub(super) warm_reps: usize,
    pub(super) timeout: Duration,
    pub(super) series: Vec<Series>,
    pub(super) methods: Vec<&'static str>,
    /// 方式ごとの最大 RSS を `/usr/bin/time -l` の別走行（計時対象外）で取る。
    pub(super) rss_probe: bool,
}

pub(super) const ALL_METHODS: [&str; 5] = [
    "single_cli",
    "rust_develop_miss",
    "rust_develop_cache_hit",
    "batch_stateless",
    "batch_decode_render",
];

// ---------------------------------------------------------------------------
// 1 リクエストの記録
// ---------------------------------------------------------------------------

struct Rec {
    method: &'static str,
    series: String,
    step: usize,
    rep: usize,
    phase: &'static str,
    wall_ms: f64,
    error_code: Option<String>,
    cache_hit: Option<bool>,
    decode_reused: Option<bool>,
    /// CLI 応答の `elapsed_ms.total`（decode + render の方式では両方の和）。キャッシュヒットは null。
    cli_total_ms: Option<f64>,
    cli_elapsed: Value,
    ci_timing: Value,
}

impl Rec {
    fn ok(&self) -> bool {
        self.error_code.is_none()
    }
    fn overhead_ms(&self) -> Option<f64> {
        self.cli_total_ms.map(|cli| self.wall_ms - cli)
    }
    fn to_json(&self) -> Value {
        json!({
            "method": self.method, "series": self.series, "step": self.step, "rep": self.rep, "phase": self.phase,
            "wall_ms": round3(self.wall_ms),
            "ok": self.ok(), "error_code": self.error_code,
            "cache_hit": self.cache_hit, "decode_reused": self.decode_reused,
            "cli_total_ms": self.cli_total_ms.map(round3),
            "transport_overhead_ms": self.overhead_ms().map(round3),
            "cli_elapsed_ms": self.cli_elapsed,
            "ci_timing": self.ci_timing,
        })
    }
}

fn ms(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}

fn cli_total(response: &Value) -> Option<f64> {
    response["elapsed_ms"]["total"].as_f64()
}

fn phase_of(rep: usize) -> &'static str {
    if rep == 0 { "cold" } else { "warm" }
}

fn rep_is_error(error: &CoreImageError) -> String {
    error.code.clone()
}

/// ps の RSS（KiB）。取れなければ None。
fn ps_rss_kib(pid: u32) -> Option<u64> {
    let out = Command::new("ps").args(["-o", "rss=", "-p", &pid.to_string()]).output().ok()?;
    String::from_utf8_lossy(&out.stdout).trim().parse().ok()
}

// ---------------------------------------------------------------------------
// 方式ごとの実行
// ---------------------------------------------------------------------------

/// 方式 1 つ × 系列 1 つの結果。
struct MethodRun {
    recs: Vec<Rec>,
    /// 最終反復の各ステップの出力 JPEG（RGB 比較用）。
    last_outputs: BTreeMap<usize, PathBuf>,
    session_starts: usize,
    session_start_ms: Vec<f64>,
    restarts: usize,
    timeouts: usize,
    rss_sampled_max_kib: Option<u64>,
    rss_probe: Value,
    notes: Vec<String>,
}

impl MethodRun {
    fn new() -> Self {
        Self {
            recs: vec![],
            last_outputs: BTreeMap::new(),
            session_starts: 0,
            session_start_ms: vec![],
            restarts: 0,
            timeouts: 0,
            rss_sampled_max_kib: None,
            rss_probe: Value::Null,
            notes: vec![],
        }
    }
    fn note_error(&mut self, code: &str) {
        if code == "SIDECAR_TIMEOUT" {
            self.timeouts += 1;
        }
    }
}

struct Ctx<'a> {
    cfg: &'a BenchConfig,
    capture_id: &'a str,
    input: &'a Path,
    series: &'a Series,
    images_dir: PathBuf,
    app_data_root: PathBuf,
}

fn request_body(input: &Path, adjustments: &Value, output: &Path) -> String {
    let mut request = base_request(input, DevelopMode::Preview, Some(adjustments));
    request.insert("output".into(), Value::String(output.to_string_lossy().into_owned()));
    Value::Object(request).to_string()
}

fn out_path(ctx: &Ctx<'_>, method: &str, step: usize) -> PathBuf {
    ctx.images_dir.join(format!("{method}-{}-{}-s{step}.jpg", ctx.capture_id, ctx.series.name))
}

fn probe_rss(ctx: &Ctx<'_>) -> Value {
    if !ctx.cfg.rss_probe {
        return json!({"measured": false, "reason": "rss_probe disabled"});
    }
    let output = out_path(ctx, "rss_probe", 0);
    let body = request_body(ctx.input, &ctx.series.steps[0], &output);
    let sidecar = ctx.cfg.sidecar.to_string_lossy().into_owned();
    match run_sidecar(Path::new("/usr/bin/time"), &["-l", &sidecar], Some(body.as_bytes()), ctx.cfg.timeout) {
        Ok(out) => {
            let mut v = parse_time_l(&out.stderr);
            v["measured"] = json!(v["max_resident_set_bytes"].is_u64());
            v["note"] = json!("単発 1 リクエストの別走行（計時対象外）。1 プロセス 1 リクエストのピーク");
            let _ = fs::remove_file(&output);
            v
        }
        Err(e) => json!({"measured": false, "reason": e.code}),
    }
}

fn run_single_cli(ctx: &Ctx<'_>) -> MethodRun {
    let mut run = MethodRun::new();
    for rep in 0..=ctx.cfg.warm_reps {
        for (step, adjustments) in ctx.series.steps.iter().enumerate() {
            let output = out_path(ctx, "single_cli", step);
            let body = request_body(ctx.input, adjustments, &output);
            let started = Instant::now();
            let result = run_sidecar(&ctx.cfg.sidecar, &[], Some(body.as_bytes()), ctx.cfg.timeout)
                .and_then(|out| {
                    let response = parse_response(&out)?;
                    if response.get("ok") != Some(&Value::Bool(true)) || !out.status.success() {
                        return Err(cli_error(&response, out.status));
                    }
                    Ok((response, out.stderr))
                });
            let wall = ms(started.elapsed());
            let mut rec = Rec {
                method: "single_cli", series: ctx.series.name.clone(), step, rep, phase: phase_of(rep),
                wall_ms: wall, error_code: None, cache_hit: None, decode_reused: None,
                cli_total_ms: None, cli_elapsed: Value::Null, ci_timing: Value::Null,
            };
            match result {
                Ok((response, stderr)) => {
                    rec.cli_total_ms = cli_total(&response);
                    rec.cli_elapsed = response["elapsed_ms"].clone();
                    rec.ci_timing = parse_ci_timing(&stderr).unwrap_or(Value::Null);
                    run.last_outputs.insert(step, output);
                }
                Err(e) => {
                    run.note_error(&e.code);
                    rec.error_code = Some(rep_is_error(&e));
                }
            }
            run.recs.push(rec);
        }
    }
    run.rss_probe = probe_rss(ctx);
    run
}

fn write_app_config(app_data: &Path, source_root: &Path) {
    fs::create_dir_all(app_data).expect("app_data");
    fs::write(
        app_data.join("config.json"),
        json!({"config_version": 1, "source_root": source_root}).to_string(),
    )
    .expect("config.json");
}

/// `rust_develop_miss` と `rust_develop_cache_hit`（同じ app_data を使い回す）を続けて走らせる。
fn run_rust_develop(ctx: &Ctx<'_>, want_miss: bool, want_hit: bool) -> (Option<MethodRun>, Option<MethodRun>) {
    let mut miss = MethodRun::new();
    let mut hit = MethodRun::new();
    let slot = DevelopSlot::new(MAX_WAITING);
    let mut last_app_data = None;
    for rep in 0..=ctx.cfg.warm_reps {
        // 反復ごとに空の app_data にして、毎回キャッシュミスにする。
        let app_data = ctx.app_data_root.join(format!("rust-{}-{}-r{rep}", ctx.capture_id, ctx.series.name));
        let _ = fs::remove_dir_all(&app_data);
        write_app_config(&app_data, &ctx.cfg.source_root);
        for (step, adjustments) in ctx.series.steps.iter().enumerate() {
            let started = Instant::now();
            let result = develop(&app_data, &ctx.cfg.sidecar, ctx.capture_id, DevelopMode::Preview,
                Some(adjustments), &slot, ctx.cfg.timeout);
            let wall = ms(started.elapsed());
            let mut rec = Rec {
                method: "rust_develop_miss", series: ctx.series.name.clone(), step, rep, phase: phase_of(rep),
                wall_ms: wall, error_code: None, cache_hit: None, decode_reused: None,
                cli_total_ms: None, cli_elapsed: Value::Null, ci_timing: Value::Null,
            };
            match result {
                Ok(r) => {
                    rec.cache_hit = Some(r.cache_hit);
                    rec.cli_total_ms = cli_total(&json!({"elapsed_ms": r.elapsed_ms}));
                    rec.cli_elapsed = r.elapsed_ms.clone();
                    if r.cache_hit {
                        // ミスのはずがヒット（鍵衝突など）。輸送比較に混ぜない。
                        rec.error_code = Some("UNEXPECTED_CACHE_HIT".into());
                    }
                    miss.last_outputs.insert(step, PathBuf::from(r.output_path));
                }
                Err(e) => {
                    miss.note_error(&e.code);
                    rec.error_code = Some(rep_is_error(&e));
                }
            }
            miss.recs.push(rec);
        }
        last_app_data = Some(app_data);
    }
    if want_hit {
        // 直前の app_data には全ステップの完成 JPEG と来歴が残っている。CLI は呼ばれない。
        if let Some(app_data) = last_app_data {
            for rep in 0..=ctx.cfg.warm_reps {
                for (step, adjustments) in ctx.series.steps.iter().enumerate() {
                    let started = Instant::now();
                    let result = develop(&app_data, &ctx.cfg.sidecar, ctx.capture_id, DevelopMode::Preview,
                        Some(adjustments), &slot, ctx.cfg.timeout);
                    let wall = ms(started.elapsed());
                    let mut rec = Rec {
                        method: "rust_develop_cache_hit", series: ctx.series.name.clone(), step, rep,
                        phase: "warm", wall_ms: wall, error_code: None, cache_hit: None,
                        decode_reused: None, cli_total_ms: None, cli_elapsed: Value::Null, ci_timing: Value::Null,
                    };
                    match result {
                        Ok(r) => {
                            rec.cache_hit = Some(r.cache_hit);
                            if !r.cache_hit {
                                rec.error_code = Some("UNEXPECTED_CACHE_MISS".into());
                            }
                            hit.last_outputs.insert(step, PathBuf::from(r.output_path));
                        }
                        Err(e) => rec.error_code = Some(rep_is_error(&e)),
                    }
                    hit.recs.push(rec);
                }
            }
        }
        hit.notes.push("CLI を呼ばない完成 JPEG のキャッシュヒット。輸送方式の比較には使わない。cli_total は意味を持たないので null".into());
    }
    miss.notes.push("develop() の ci_timing は stderr を捨てるため欠測。段階内訳は single_cli の ci_timing を参照".into());
    miss.notes.push("子プロセスは single_cli と同じ。RSS は single_cli の probe を参照（rust_develop 自体は欠測）".into());
    miss.notes.push("cli_version()（--version 起動）と sidecar_sha256（バイナリ全読み）は run_bench の開始時に実行済みで cold の wall に入らない。アプリ起動直後の初回現像ではこの分が加わる（未測定）".into());
    (want_miss.then_some(miss), want_hit.then_some(hit))
}

/// `BatchSession` を持ち回し、失敗・タイムアウトで使えなくなったら次の要求の前に起動し直す。
struct BatchDriver<'a> {
    cfg: &'a BenchConfig,
    session: Option<batch::BatchSession>,
}

impl<'a> BatchDriver<'a> {
    fn new(cfg: &'a BenchConfig) -> Self {
        Self { cfg, session: None }
    }
    fn session(&mut self, run: &mut MethodRun) -> Result<&mut batch::BatchSession, CoreImageError> {
        if self.session.as_ref().is_none_or(|s| !s.is_usable()) {
            let had_session = self.session.is_some() || run.session_starts > 0;
            let started = Instant::now();
            let session = batch::BatchSession::start(&self.cfg.sidecar, self.cfg.timeout)?;
            run.session_start_ms.push(ms(started.elapsed()));
            run.session_starts += 1;
            if had_session {
                run.restarts += 1;
            }
            self.session = Some(session);
        }
        Ok(self.session.as_mut().expect("session started"))
    }
    fn sample_rss(&mut self, run: &mut MethodRun) {
        if let Some(pid) = self.session.as_ref().and_then(|s| s.pid()) {
            if let Some(kib) = ps_rss_kib(pid) {
                run.rss_sampled_max_kib = Some(run.rss_sampled_max_kib.map_or(kib, |m| m.max(kib)));
            }
        }
    }
    fn finish(mut self) -> Option<bool> {
        self.session.take().and_then(|s| s.finish().ok()).map(|status| status.success())
    }
}

fn run_batch_stateless(ctx: &Ctx<'_>) -> MethodRun {
    let mut run = MethodRun::new();
    let mut driver = BatchDriver::new(ctx.cfg);
    for rep in 0..=ctx.cfg.warm_reps {
        for (step, adjustments) in ctx.series.steps.iter().enumerate() {
            let output = out_path(ctx, "batch_stateless", step);
            let mut request = base_request(ctx.input, DevelopMode::Preview, Some(adjustments));
            request.insert("output".into(), Value::String(output.to_string_lossy().into_owned()));
            let request = Value::Object(request);
            let session = match driver.session(&mut run) {
                Ok(s) => s,
                Err(e) => {
                    run.note_error(&e.code);
                    run.recs.push(failed_rec("batch_stateless", ctx, step, rep, &e));
                    continue;
                }
            };
            let started = Instant::now();
            let result = session.request(&request);
            let wall = ms(started.elapsed());
            let mut rec = new_rec("batch_stateless", ctx, step, rep, wall);
            match result {
                Ok(response) => {
                    rec.cli_total_ms = cli_total(&response);
                    rec.cli_elapsed = response["elapsed_ms"].clone();
                    run.last_outputs.insert(step, output);
                }
                Err(e) => {
                    run.note_error(&e.code);
                    rec.error_code = Some(rep_is_error(&e));
                }
            }
            driver.sample_rss(&mut run);
            run.recs.push(rec);
        }
    }
    run.notes.push("ci_timing は欠測: BatchSession の stderr_tail は非公開で、1 件ごとの GEOFRAME_CI_TIMING 行を取り出せない（応答の elapsed_ms 3 値で代用）".into());
    run.notes.push("RSS は各要求直後の ps サンプル（要求境界の下限値。ピークではない）".into());
    if driver.finish() == Some(false) {
        run.notes.push("batch プロセスが非 0 で終了した".into());
    }
    run
}

fn new_rec(method: &'static str, ctx: &Ctx<'_>, step: usize, rep: usize, wall_ms: f64) -> Rec {
    Rec {
        method, series: ctx.series.name.clone(), step, rep, phase: phase_of(rep), wall_ms,
        error_code: None, cache_hit: None, decode_reused: None, cli_total_ms: None,
        cli_elapsed: Value::Null, ci_timing: Value::Null,
    }
}

fn failed_rec(method: &'static str, ctx: &Ctx<'_>, step: usize, rep: usize, e: &CoreImageError) -> Rec {
    let mut rec = new_rec(method, ctx, step, rep, 0.0);
    rec.error_code = Some(rep_is_error(e));
    rec
}

fn run_batch_decode_render(ctx: &Ctx<'_>) -> MethodRun {
    let mut run = MethodRun::new();
    let mut driver = BatchDriver::new(ctx.cfg);
    // (decode_key, handle)。セッションが作り直されたら無効。
    let mut held: Option<(String, String)> = None;
    for rep in 0..=ctx.cfg.warm_reps {
        for (step, adjustments) in ctx.series.steps.iter().enumerate() {
            let output = out_path(ctx, "batch_decode_render", step);
            let key = decode_key(adjustments);
            let starts_before = run.session_starts;
            let session = match driver.session(&mut run) {
                Ok(s) => s,
                Err(e) => {
                    run.note_error(&e.code);
                    run.recs.push(failed_rec("batch_decode_render", ctx, step, rep, &e));
                    held = None;
                    continue;
                }
            };
            if run.session_starts != starts_before {
                held = None;
            }
            let started = Instant::now();
            let mut cli_ms = 0.0;
            let mut reused = false;
            let mut decode_elapsed = Value::Null;
            let outcome: Result<Value, CoreImageError> = (|| {
                if held.as_ref().is_some_and(|(k, _)| *k == key) {
                    reused = true;
                } else {
                    if let Some((_, old)) = held.take() {
                        // 古いハンドルの解放も、その要求の壁時計に含める（本番のハンドル管理と同じ）。
                        session.release(&old)?;
                    }
                    let mut request = base_request(ctx.input, DevelopMode::Preview, Some(adjustments));
                    request.remove("format");
                    let decoded = session.decode(&Value::Object(request))?;
                    cli_ms += cli_total(&decoded.response).unwrap_or(0.0);
                    decode_elapsed = decoded.response["elapsed_ms"].clone();
                    held = Some((key.clone(), decoded.id));
                }
                let handle = held.as_ref().map(|(_, h)| h.clone()).expect("held handle");
                let request = json!({
                    "output": output, "format": "jpeg", "adjustments": adjustments
                });
                let rendered = session.render(&handle, &request);
                if let Err(e) = &rendered {
                    if batch::is_handle_lost(e) {
                        held = None;
                    }
                }
                rendered
            })();
            let wall = ms(started.elapsed());
            let mut rec = new_rec("batch_decode_render", ctx, step, rep, wall);
            rec.decode_reused = Some(reused);
            match outcome {
                Ok(response) => {
                    cli_ms += cli_total(&response).unwrap_or(0.0);
                    rec.cli_total_ms = Some(cli_ms);
                    rec.cli_elapsed = json!({"decode": decode_elapsed, "render": response["elapsed_ms"]});
                    run.last_outputs.insert(step, output);
                }
                Err(e) => {
                    run.note_error(&e.code);
                    rec.error_code = Some(rep_is_error(&e));
                    if driver.session.as_ref().is_some_and(|s| !s.is_usable()) {
                        held = None;
                    }
                }
            }
            driver.sample_rss(&mut run);
            run.recs.push(rec);
        }
        // 反復の最後にハンドルを解放する（計時対象外）。次の反復はデコードから始める。
        if let (Some((_, handle)), Some(session)) = (held.take(), driver.session.as_mut()) {
            let _ = session.release(&handle);
        }
    }
    run.notes.push("ci_timing は欠測（batch_stateless と同じ理由）".into());
    run.notes.push("反復の先頭ステップは必ずデコードする（ハンドルは反復末尾で解放）。decode_reused=true は後段だけの追従".into());
    run.notes.push("render は保持した half float 画素から出すため、single の JPEG と画素が一致しないことがある（maxdiff を記録）".into());
    if driver.finish() == Some(false) {
        run.notes.push("batch プロセスが非 0 で終了した".into());
    }
    run
}

// ---------------------------------------------------------------------------
// 集計
// ---------------------------------------------------------------------------

fn summarize(run: &MethodRun, steps: usize) -> Value {
    let mut phases = Map::new();
    for phase in ["cold", "warm"] {
        let ok: Vec<&Rec> = run.recs.iter().filter(|r| r.phase == phase && r.ok()).collect();
        if run.recs.iter().all(|r| r.phase != phase) {
            continue;
        }
        let walls: Vec<f64> = ok.iter().map(|r| r.wall_ms).collect();
        let overheads: Vec<f64> = ok.iter().filter_map(|r| r.overhead_ms()).collect();
        let cli: Vec<f64> = ok.iter().filter_map(|r| r.cli_total_ms).collect();
        // 系列 wall = 1 反復の全ステップの wall 合計（全ステップ成功の反復だけ）。
        let mut by_rep: BTreeMap<usize, (usize, f64)> = BTreeMap::new();
        for r in &ok {
            let e = by_rep.entry(r.rep).or_insert((0, 0.0));
            e.0 += 1;
            e.1 += r.wall_ms;
        }
        let series_walls: Vec<f64> = by_rep.values().filter(|(n, _)| *n == steps).map(|(_, w)| *w).collect();
        let mut entry = json!({
            "request_wall_ms": stats(&walls),
            "transport_overhead_ms": stats(&overheads),
            "cli_total_ms": stats(&cli),
            "series_wall_ms": stats(&series_walls),
        });
        if ok.iter().any(|r| r.decode_reused.is_some()) {
            let split = |reused: bool| -> Vec<f64> {
                ok.iter().filter(|r| r.decode_reused == Some(reused)).map(|r| r.wall_ms).collect()
            };
            entry["request_wall_ms_decode_miss"] = stats(&split(false));
            entry["request_wall_ms_decode_reused"] = stats(&split(true));
        }
        let timing_keys = ["create", "configure", "graph", "render", "encode_write", "other", "total"];
        if ok.iter().any(|r| !r.ci_timing.is_null()) {
            let mut t = Map::new();
            for key in timing_keys {
                let v: Vec<f64> = ok.iter().filter_map(|r| r.ci_timing[key].as_f64()).collect();
                t.insert(key.to_string(), stats(&v));
            }
            entry["ci_timing_ms"] = Value::Object(t);
        }
        phases.insert(phase.to_string(), entry);
    }
    let errors: Vec<Value> = run.recs.iter().filter(|r| !r.ok())
        .map(|r| json!({"series": r.series, "step": r.step, "rep": r.rep, "code": r.error_code}))
        .collect();
    json!({
        "phases": Value::Object(phases),
        "requests_total": run.recs.len(),
        "errors": errors,
        "timeouts": run.timeouts,
        "restarts": run.restarts,
        "session_starts": run.session_starts,
        "session_start_ms": run.session_start_ms.iter().map(|v| round3(*v)).collect::<Vec<_>>(),
        "rss": {
            "sampled_max_kib_at_request_boundaries": run.rss_sampled_max_kib,
            "max_resident_probe": run.rss_probe,
        },
        "notes": run.notes,
    })
}

fn definitions() -> Value {
    json!({
        "wall": "Rust プロセス内 Instant。React / Tauri IPC / WebView 表示は含まない。E2E の代用にしない",
        "transport_overhead_ms": "wall − CLI 応答の elapsed_ms.total（decode+render 方式は両方の和）。プロセス起動・JSON・待ち・ファイル書き込み・(rust_develop は解決/鍵/来歴/prune)を含む",
        "cold": "その方式×系列の最初の反復。batch の起動・初期化は先頭要求の wall に含む。固定順で実行し、OS/GPU の cold は保証しないため方式間 cold 比較には使わない",
        "session_start_ms": "spawn 呼び出しだけの時間。ready/初期化を待たず、それらは cold 先頭要求の wall に含む",
        "warm": "同じ方式×系列の 2 回目以降の反復（batch は同じセッション、OS ファイルキャッシュは温まっている可能性）",
        "series_wall": "1 反復の全ステップの wall 合計（全ステップ成功の反復のみ）。cold は n=1",
        "percentiles": "最近順位法。n < 20 は small_sample=true で p95 は実質 max",
        "rust_develop_cache_hit": "CLI を呼ばない完成 JPEG のキャッシュヒット。輸送比較に混ぜない",
        "rgb_compare": "single_cli の出力 JPEG をデコードした RGB との比較。閾値は未決で合否にしない",
        "jpeg_quality": "全方式とも jpeg_quality を送らない（CLI 既定 0.92 = 本番 Rust と同じ）",
        "max_edge": PREVIEW_MAX_EDGE,
        "poll": "run_sidecar（single_cli と rust_develop）は子の終了を 25ms 間隔でポーリングするため、transport_overhead に最大 25ms の量子化が入る（本番と同じ）。BatchSession は応答待ちでブロックする",
        "slot": "rust_develop は DevelopSlot(待ち 2)・timeout はベンチ設定値（既定 60 秒）。単独実行のため BUSY 待ちは発生しない",
    })
}

fn unmeasured() -> Value {
    json!([
        "React / Tauri IPC / WebView 表示を含む E2E（Stage1 の UI フックで測る）",
        "スライダー入力 → 数値表示の応答（画像 E2E と別。本ベンチは測らない）",
        "OS ファイルキャッシュ・GPU の cold",
        "連続ドラッグ中の同時実行・BUSY・古いリクエスト破棄",
        "Swift ライブラリ（FFI/XPC）。本ベンチは Stage0（単発 CLI vs 既存 batch）のみ",
        "rust_develop / batch 方式の GEOFRAME_CI_TIMING 段階内訳（stderr を取れない。single_cli のみ）",
        "batch のピーク RSS（要求境界のサンプル値のみ）",
        "画質同等性の合否（閾値未決。maxdiff の記録のみ）",
    ])
}

fn filesystem_evidence(path: &Path, label: &str) -> Value {
    let canonical = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    let mount = Command::new("/sbin/mount").output().ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned());
    let detected = mount.as_deref().and_then(|m| mount_fstype(m, &canonical));
    let path_digest: String = Sha256::digest(canonical.to_string_lossy().as_bytes())
        .iter().take(4).map(|b| format!("{b:02x}")).collect();
    let (fstype, is_network, root) = match &detected {
        Some((t, root)) => (Some(t.clone()), Some(is_network_fstype(t)), Some(*root)),
        None => (None, None, None),
    };
    // ラベルが smb/nfs を名乗るのにローカル FS、またはその逆なら警告（実行は止めない）。
    let label_lc = label.to_lowercase();
    let claims_network = ["smb", "nfs", "network", "nas"].iter().any(|k| label_lc.contains(k));
    let mismatch = is_network.map(|n| n != claims_network && (claims_network || label_lc.contains("local")));
    json!({
        "user_label": label,
        "detected_fstype": fstype,
        "detected_is_network": is_network,
        "mount_point_is_root": root,
        "label_mismatch_warning": mismatch.unwrap_or(false),
        "source_path_sha8": path_digest,
        "note": "fstype は mount 出力の最長一致。デバイス名・マウントパスは私的情報を含みうるので記録しない",
    })
}

// ---------------------------------------------------------------------------
// 全体
// ---------------------------------------------------------------------------

pub(super) fn run_bench(cfg: &BenchConfig) -> Result<Value, CoreImageError> {
    fs::create_dir_all(&cfg.out_dir).map_err(|e| CoreImageError::new("BENCH_IO", e.to_string()))?;
    let images_dir = cfg.out_dir.join("images");
    let app_data_root = cfg.out_dir.join("app_data");
    for dir in [&images_dir, &app_data_root] {
        fs::create_dir_all(dir).map_err(|e| CoreImageError::new("BENCH_IO", e.to_string()))?;
    }
    let version = cli_version(&cfg.sidecar)?;
    let self_rss_start = ps_rss_kib(std::process::id());
    let mut sources = vec![];
    let mut captures = vec![];
    for capture_id in &cfg.capture_ids {
        let input = resolve_arw(&cfg.source_root, capture_id)?;
        let size = fs::metadata(&input).map(|m| m.len()).unwrap_or(0);
        sources.push(json!({"capture_id": capture_id, "arw_size_bytes": size}));
        let mut series_out = vec![];
        for series in &cfg.series {
            let ctx = Ctx { cfg, capture_id, input: &input, series, images_dir: images_dir.clone(),
                app_data_root: app_data_root.clone() };
            let mut methods = Map::new();
            let mut reference: Option<BTreeMap<usize, PathBuf>> = None;
            for method in ALL_METHODS {
                if !cfg.methods.contains(&method) {
                    continue;
                }
                let runs: Vec<(&str, MethodRun)> = match method {
                    "single_cli" => vec![("single_cli", run_single_cli(&ctx))],
                    "rust_develop_miss" | "rust_develop_cache_hit" => {
                        // 2 つは同じ走行から作る。先に来た方で両方走らせ、後の方は結果を再利用する。
                        if method == "rust_develop_cache_hit" && cfg.methods.contains(&"rust_develop_miss") {
                            continue;
                        }
                        let (miss, hit) = run_rust_develop(
                            &ctx,
                            cfg.methods.contains(&"rust_develop_miss"),
                            cfg.methods.contains(&"rust_develop_cache_hit"),
                        );
                        let mut v = vec![];
                        if let Some(m) = miss { v.push(("rust_develop_miss", m)); }
                        if let Some(h) = hit { v.push(("rust_develop_cache_hit", h)); }
                        v
                    }
                    "batch_stateless" => vec![("batch_stateless", run_batch_stateless(&ctx))],
                    _ => vec![("batch_decode_render", run_batch_decode_render(&ctx))],
                };
                for (name, run) in runs {
                    if name == "single_cli" {
                        reference = Some(run.last_outputs.clone());
                    }
                    let mut entry = summarize(&run, series.steps.len());
                    entry["measurement_label"] = json!(if name == "rust_develop_cache_hit" {
                        "rust_complete_jpeg_cache_hit (not transport)"
                    } else {
                        "transport_incl_cli_work"
                    });
                    entry["requests"] = Value::Array(run.recs.iter().map(Rec::to_json).collect());
                    // single_cli を基準にした出力 RGB の比較（ステップごと）。
                    let compare: Vec<Value> = match (&reference, name) {
                        (Some(reference), n) if n != "single_cli" => run.last_outputs.iter()
                            .map(|(step, path)| match reference.get(step) {
                                Some(base) => {
                                    let mut c = compare_rgb(base, path);
                                    c["step"] = json!(step);
                                    c["output_sha12"] = json!(sha256_hex12(path));
                                    c
                                }
                                None => json!({"step": step, "comparable": false, "reason": "no single_cli reference"}),
                            })
                            .collect(),
                        _ => vec![],
                    };
                    entry["rgb_vs_single_cli"] = Value::Array(compare);
                    methods.insert(name.to_string(), entry);
                }
            }
            series_out.push(json!({
                "series": series.name, "class": series.class.as_str(),
                "steps": series.steps.len(), "methods": Value::Object(methods),
            }));
        }
        captures.push(json!({"capture_id": capture_id, "series": series_out}));
    }
    Ok(json!({
        "schema": "geoframe.transport-bench.v1",
        "generated_at_unix": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0),
        "build_profile": if cfg!(debug_assertions) { "debug" } else { "release" },
        "cli": {"cli_version": version.cli_version, "macos_version": version.macos_version,
                "binary_sha256_12": version.binary_sha256.chars().take(12).collect::<String>()},
        "environment": {
            "storage": filesystem_evidence(&cfg.source_root, &cfg.storage_label),
            "warm_reps": cfg.warm_reps,
            "timeout_s": cfg.timeout.as_secs_f64(),
            "ci_timing_env": std::env::var("GEOFRAME_CI_TIMING").ok(),
            "base_adjustments": base_adjustments(),
            "bench_process_rss_kib_start": self_rss_start,
            "bench_process_rss_kib_end": ps_rss_kib(std::process::id()),
        },
        "definitions": definitions(),
        "unmeasured": unmeasured(),
        "sources": sources,
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
                "geoframe-bench141-{tag}-{}-{}", std::process::id(), N.fetch_add(1, Ordering::SeqCst)));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> &Path { &self.0 }
    }
    impl Drop for Dir {
        fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
    }

    #[test]
    fn standard_series_have_the_declared_decode_behaviour() {
        let all = standard_series();
        assert!(all.iter().any(|s| s.class == SeriesClass::DecodeChanging));
        assert!(all.iter().any(|s| s.class == SeriesClass::PostOnly));
        for series in &all {
            let keys: Vec<String> = series.steps.iter().map(decode_key).collect();
            let distinct_keys: std::collections::HashSet<_> = keys.iter().collect();
            let distinct_steps: std::collections::HashSet<_> =
                series.steps.iter().map(|s| s.to_string()).collect();
            // 同じ調整を 2 度出すと rust_develop が意図せずキャッシュヒットする。
            assert_eq!(distinct_steps.len(), series.steps.len(), "{}", series.name);
            match series.class {
                SeriesClass::DecodeChanging => {
                    assert_eq!(distinct_keys.len(), keys.len(), "{}: decode key が重複", series.name)
                }
                SeriesClass::PostOnly => {
                    assert_eq!(distinct_keys.len(), 1, "{}: decode key が動いている", series.name)
                }
            }
        }
        for name in ["decode_exposure", "decode_highlight_recovery", "decode_wb_temp",
            "decode_min_sharpness", "decode_noise_reduction", "post_shadow_lift",
            "post_brightness", "post_contrast", "post_saturation", "post_sharpness_gt1"] {
            assert!(all.iter().any(|s| s.name == name), "{name}");
        }
    }

    #[test]
    fn percentile_stats_use_nearest_rank_and_flag_small_samples() {
        let v: Vec<f64> = (1..=10).map(f64::from).collect();
        let s = stats(&v);
        assert_eq!(s["p50"], json!(5.0));
        assert_eq!(s["p95"], json!(10.0));
        assert_eq!(s["n"], json!(10));
        assert_eq!(s["small_sample"], json!(true));
        assert_eq!(stats(&[]), json!({"n": 0}));
        let big: Vec<f64> = (1..=100).map(f64::from).collect();
        assert_eq!(stats(&big)["p95"], json!(95.0));
        assert_eq!(stats(&big)["small_sample"], json!(false));
    }

    #[test]
    fn ci_timing_line_is_parsed() {
        let line = "noise\ngeoframe-ci-develop: timing create=62.6 configure=14.3 graph=0.8 render=393.5 encode_write=4.0 other=0.1 total=475.3\n";
        let t = parse_ci_timing(line).unwrap();
        assert_eq!(t["render"], json!(393.5));
        assert_eq!(t["total"], json!(475.3));
        assert!(parse_ci_timing("nothing here").is_none());
    }

    #[test]
    fn mount_output_picks_longest_prefix_and_never_leaks_device() {
        let mount = "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n\
            /dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, protect)\n\
            //alice@nas.local/Backup on /Volumes/Backup (smbfs, nodev, nosuid, mounted by alice)\n";
        let (t, root) = mount_fstype(mount, Path::new("/Volumes/Backup/100MSDCF")).unwrap();
        assert_eq!((t.as_str(), root), ("smbfs", false));
        assert!(is_network_fstype(&t));
        let (t, root) = mount_fstype(mount, Path::new("/Users/x/raw")).unwrap();
        assert_eq!((t.as_str(), root), ("apfs", true));
        assert!(!is_network_fstype(&t));
        let (t, _) = mount_fstype(mount, Path::new("/System/Volumes/Data/Users/x")).unwrap();
        assert_eq!(t, "apfs");
        let evidence = filesystem_evidence(Path::new("/"), "local");
        assert!(!evidence.to_string().contains("alice"));
    }

    #[test]
    fn time_l_output_is_parsed() {
        let stderr = "        1.40 real         0.92 user         0.20 sys\n 289538048  maximum resident set size\n  1086000000  peak memory footprint\n";
        let v = parse_time_l(stderr);
        assert_eq!(v["max_resident_set_bytes"], json!(289538048u64));
        assert_eq!(v["peak_memory_footprint_bytes"], json!(1086000000u64));
    }

    fn write_jpeg(path: &Path, shift: u8) {
        let img = image::RgbImage::from_fn(16, 16, |x, y| {
            image::Rgb([(x * 16) as u8, (y * 16) as u8, 128u8.wrapping_add(shift)])
        });
        img.save(path).unwrap();
    }

    #[test]
    fn rgb_compare_reports_identical_and_different() {
        let dir = Dir::new("rgb");
        let (a, b, c) = (dir.path().join("a.jpg"), dir.path().join("b.jpg"), dir.path().join("c.jpg"));
        write_jpeg(&a, 0);
        fs::copy(&a, &b).unwrap();
        write_jpeg(&c, 60);
        let same = compare_rgb(&a, &b);
        assert_eq!(same["rawpixels_identical"], json!(true));
        assert_eq!(same["max_abs_diff"], json!(0));
        let diff = compare_rgb(&a, &c);
        assert_eq!(diff["rawpixels_identical"], json!(false));
        assert!(diff["max_abs_diff"].as_u64().unwrap() > 0);
        assert_eq!(compare_rgb(&a, &dir.path().join("missing.jpg"))["comparable"], json!(false));
    }

    /// 本物の CLI の代わりをする sh スクリプト。`--version` / 単発 / `--batch`（現像・decode・render・release）を話す。
    /// `exposure:1.5` を含む要求は失敗応答、`exposure:1.25` を含む要求は 5 秒ハングする。
    /// `render` op は `alt` の JPEG を書く（decode+render が single と画素差を持つ状況の再現）。
    fn fake_cli(dir: &Path, jpg: &Path, alt: &Path) -> PathBuf {
        let script = format!(r##"#!/bin/sh
JPG='{jpg}'
ALT='{alt}'
timing() {{ echo "geoframe-ci-develop: timing create=1.0 configure=2.0 graph=0.5 render=3.0 encode_write=1.0 other=0.1 total=7.6" >&2; }}
ok() {{
  printf '{{"ok":true,"cli_version":"0.8.0","macos_version":"27.0.1","decoder_version":"9","mode":"preview","format":"jpeg","output":"%s","width":16,"height":16,"elapsed_ms":{{"decode":1.0,"render_encode":2.0,"total":3.0}},"applied":{{"lens_correction_enabled":true}},"source":{{}}%s}}\n' "$1" "$2"
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
      *'"exposure":1.25,'*) sleep 5 ;;
      *'"exposure":1.5,'*) printf '{{"error":{{"code":"RENDER_FAILED","message":"boom"}},"ok":false,"sequence":%d}}\n' "$n" ;;
      *'"op":"decode"'*) printf '{{"ok":true,"op":"decode","handle":"h%d","width":16,"height":16,"bytes":10,"elapsed_ms":{{"decode":1.0,"render_encode":0.5,"total":1.5}},"sequence":%d}}\n' "$n" "$n" ;;
      *'"op":"release"'*) printf '{{"ok":true,"op":"release","released":true,"sequence":%d}}\n' "$n" ;;
      *'"op":"render"'*) cp "$ALT" "$out"; ok "$out" ",\"op\":\"render\",\"sequence\":$n" ;;
      *) cp "$JPG" "$out"; ok "$out" ",\"sequence\":$n" ;;
    esac
  done
  exit 0
fi
req=$(cat)
out=$(printf '%s' "$req" | sed -n 's/.*"output":"\([^"]*\)".*/\1/p')
case "$req" in
  *'"exposure":1.25,'*) sleep 5 ;;
  *'"exposure":1.5,'*) printf '%s\n' '{{"error":{{"code":"RENDER_FAILED","message":"boom"}},"ok":false}}'; exit 1 ;;
esac
cp "$JPG" "$out"
timing
ok "$out" ""
"##, jpg = jpg.display(), alt = alt.display());
        let path = dir.join("fake-cli");
        fs::write(&path, script).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn fake_setup(tag: &str) -> (Dir, BenchConfig) {
        let dir = Dir::new(tag);
        let (jpg, alt) = (dir.path().join("a.jpg"), dir.path().join("alt.jpg"));
        write_jpeg(&jpg, 0);
        write_jpeg(&alt, 80);
        let source = dir.path().join("src");
        fs::create_dir_all(&source).unwrap();
        fs::write(source.join("DSC00001.ARW"), b"fake raw").unwrap();
        let cfg = BenchConfig {
            sidecar: fake_cli(dir.path(), &jpg, &alt),
            source_root: source,
            capture_ids: vec!["DSC00001".into()],
            out_dir: dir.path().join("out"),
            storage_label: "local-test".into(),
            warm_reps: 2,
            timeout: Duration::from_secs(2),
            series: vec![
                numeric_series("t_decode", SeriesClass::DecodeChanging, "exposure", &[0.0, 0.5]),
                numeric_series("t_post", SeriesClass::PostOnly, "shadow_lift", &[10.0, 20.0, 30.0]),
            ],
            methods: ALL_METHODS.to_vec(),
            rss_probe: false,
        };
        (dir, cfg)
    }

    fn method<'a>(report: &'a Value, series: &str, name: &str) -> &'a Value {
        report["captures"][0]["series"].as_array().unwrap().iter()
            .find(|s| s["series"] == json!(series)).unwrap()["methods"].get(name)
            .unwrap_or_else(|| panic!("{series}/{name} missing"))
    }

    #[test]
    fn fake_cli_contract_all_methods_labels_and_rgb() {
        let (_dir, cfg) = fake_setup("contract");
        let report = run_bench(&cfg).unwrap();
        assert_eq!(report["schema"], json!("geoframe.transport-bench.v1"));
        assert_eq!(report["environment"]["storage"]["user_label"], json!("local-test"));
        let text = report.to_string();
        // 私的な絶対パス（一時ディレクトリ）が JSON に漏れていない。
        assert!(!text.contains(&std::env::temp_dir().to_string_lossy().into_owned()), "path leaked");
        for series in ["t_decode", "t_post"] {
            for name in ALL_METHODS {
                let m = method(&report, series, name);
                assert_eq!(m["errors"], json!([]), "{series}/{name}");
                let label = m["measurement_label"].as_str().unwrap();
                assert_eq!(label.contains("not transport"), name == "rust_develop_cache_hit");
            }
            // cold は 1 反復、warm は warm_reps 反復
            let single = method(&report, series, "single_cli");
            let steps = if series == "t_decode" { 2 } else { 3 };
            assert_eq!(single["phases"]["cold"]["request_wall_ms"]["n"], json!(steps));
            assert_eq!(single["phases"]["warm"]["request_wall_ms"]["n"], json!(steps * 2));
            assert_eq!(single["phases"]["warm"]["series_wall_ms"]["n"], json!(2));
            // single_cli だけ ci_timing を持つ
            assert_eq!(single["phases"]["cold"]["ci_timing_ms"]["render"]["p50"], json!(3.0));
            assert!(method(&report, series, "batch_stateless")["phases"]["cold"].get("ci_timing_ms").is_none());
            // キャッシュヒットは CLI 時間を持たない（輸送比較の統計に入らない）
            let hit = method(&report, series, "rust_develop_cache_hit");
            assert_eq!(hit["phases"]["warm"]["cli_total_ms"], json!({"n": 0}));
            assert!(hit["requests"].as_array().unwrap().iter().all(|r| r["cache_hit"] == json!(true)));
            let miss = method(&report, series, "rust_develop_miss");
            assert!(miss["requests"].as_array().unwrap().iter().all(|r| r["cache_hit"] == json!(false)));
            // single / rust_develop / batch_stateless は同じ JPEG → RGB 一致。decode+render は alt を書く → 不一致。
            for name in ["rust_develop_miss", "rust_develop_cache_hit", "batch_stateless"] {
                for c in method(&report, series, name)["rgb_vs_single_cli"].as_array().unwrap() {
                    assert_eq!(c["rawpixels_identical"], json!(true), "{series}/{name}");
                }
            }
            for c in method(&report, series, "batch_decode_render")["rgb_vs_single_cli"].as_array().unwrap() {
                assert_eq!(c["rawpixels_identical"], json!(false));
                assert!(c["max_abs_diff"].as_u64().unwrap() > 0);
            }
        }
        // 後段だけの系列: 反復の先頭でデコード、残りは再利用。decode 系列は毎回デコード。
        let post = method(&report, "t_post", "batch_decode_render");
        assert_eq!(post["phases"]["cold"]["request_wall_ms_decode_miss"]["n"], json!(1));
        assert_eq!(post["phases"]["cold"]["request_wall_ms_decode_reused"]["n"], json!(2));
        let dec = method(&report, "t_decode", "batch_decode_render");
        assert_eq!(dec["phases"]["warm"]["request_wall_ms_decode_reused"]["n"], json!(0));
        // cold は新規セッション 1 回、warm は同じセッション
        assert_eq!(post["session_starts"], json!(1));
        assert_eq!(post["restarts"], json!(0));
        // 輸送オーバーヘッド = wall − cli_total を持つ
        assert!(method(&report, "t_post", "single_cli")["phases"]["warm"]["transport_overhead_ms"]["n"].as_u64().unwrap() > 0);
        assert!(post["rss"]["sampled_max_kib_at_request_boundaries"].as_u64().is_some());
        assert!(report["unmeasured"].as_array().unwrap().len() >= 5);
    }

    #[test]
    fn fake_cli_errors_and_timeouts_are_recorded_without_aborting() {
        let (_dir, mut cfg) = fake_setup("errors");
        cfg.warm_reps = 0;
        cfg.timeout = Duration::from_secs(1);
        cfg.methods = vec!["single_cli", "batch_stateless", "batch_decode_render", "rust_develop_miss"];
        // ok → 失敗 → ハング → ok の順
        cfg.series = vec![numeric_series("t_err", SeriesClass::DecodeChanging, "exposure", &[0.0, 1.5, 1.25, -0.5])];
        let report = run_bench(&cfg).unwrap();
        for name in ["single_cli", "batch_stateless", "batch_decode_render", "rust_develop_miss"] {
            let m = method(&report, "t_err", name);
            let errors = m["errors"].as_array().unwrap();
            let codes: Vec<&str> = errors.iter().map(|e| e["code"].as_str().unwrap()).collect();
            assert_eq!(codes, vec!["RENDER_FAILED", "SIDECAR_TIMEOUT"], "{name}");
            assert_eq!(m["timeouts"], json!(1), "{name}");
            // 失敗後も系列は最後のステップまで続く（成功は 0 と 3 の 2 件）
            assert_eq!(m["phases"]["cold"]["request_wall_ms"]["n"], json!(2), "{name}");
            // 全ステップ成功でない反復は series_wall に入らない
            assert_eq!(m["phases"]["cold"]["series_wall_ms"], json!({"n": 0}), "{name}");
        }
        // batch はタイムアウトでプロセスを kill → 次の要求の前に再起動
        assert_eq!(method(&report, "t_err", "batch_stateless")["restarts"], json!(1));
        assert_eq!(method(&report, "t_err", "batch_decode_render")["restarts"], json!(1));
        assert_eq!(method(&report, "t_err", "single_cli")["restarts"], json!(0));
    }

    /// 実素材ベンチ（親が実行）。手順: docs/reports/coreimage-preview-transport-bench.md
    #[test]
    #[ignore]
    fn coreimage_transport_bench() {
        assert!(!cfg!(debug_assertions), "--release で実行してください");
        let env = |name: &str| std::env::var(format!("GEOFRAME_TRANSPORT_BENCH_{name}")).ok().filter(|v| !v.trim().is_empty());
        let need = |name: &str| env(name).unwrap_or_else(|| panic!("GEOFRAME_TRANSPORT_BENCH_{name} を指定してください"));
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let sidecar = env("SIDECAR").map(PathBuf::from)
            .unwrap_or_else(|| repo.join("src-tauri/binaries/geoframe-ci-develop-aarch64-apple-darwin"));
        let size = fs::metadata(&sidecar).map(|m| m.len()).unwrap_or(0);
        assert!(size > 100_000, "{} は実 CLI ではありません（スタブ?）。npm run build:coreimage の成果物を GEOFRAME_TRANSPORT_BENCH_SIDECAR で指定してください", sidecar.display());
        let source_root = PathBuf::from(need("SOURCE_ROOT")).canonicalize().expect("SOURCE_ROOT");
        let capture_ids: Vec<String> = need("CAPTURES").split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
        assert!(!capture_ids.is_empty() && capture_ids.iter().all(|c| is_valid_capture_id(c)), "CAPTURES は DSC00000 形式をカンマ区切り");
        let out_dir = PathBuf::from(need("OUT"));
        let storage_label = need("STORAGE_LABEL");
        let ci_timing = env("CI_TIMING").is_none_or(|v| v != "0");
        if ci_timing {
            // 子プロセス（run_sidecar / BatchSession 経由を含む）へ継承させる。ベンチは 1 スレッドで走る前提。
            std::env::set_var("GEOFRAME_CI_TIMING", "1");
        }
        let filter = |name: &str| env(name).map(|v| v.split(',').map(|s| s.trim().to_string()).collect::<Vec<_>>());
        let mut series = standard_series();
        if let Some(names) = filter("SERIES") {
            series.retain(|s| names.contains(&s.name));
            assert!(!series.is_empty(), "SERIES に一致する系列がありません");
        }
        let methods: Vec<&'static str> = match filter("METHODS") {
            Some(names) => ALL_METHODS.iter().copied().filter(|m| names.iter().any(|n| n == m)).collect(),
            None => ALL_METHODS.to_vec(),
        };
        let cfg = BenchConfig {
            sidecar, source_root, capture_ids, out_dir: out_dir.clone(), storage_label: storage_label.clone(),
            warm_reps: env("WARM").map_or(3, |v| v.parse().expect("WARM")),
            timeout: Duration::from_secs(env("TIMEOUT_S").map_or(60, |v| v.parse().expect("TIMEOUT_S"))),
            series, methods, rss_probe: env("RSS_PROBE").is_none_or(|v| v != "0"),
        };
        let report = run_bench(&cfg).expect("bench");
        let unix = report["generated_at_unix"].as_u64().unwrap_or(0);
        let label: String = storage_label.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
        let path = out_dir.join(format!("transport-bench-{label}-{unix}.json"));
        fs::write(&path, serde_json::to_string_pretty(&report).unwrap() + "\n").unwrap();
        println!("結果: {}", path.display());
    }
}
