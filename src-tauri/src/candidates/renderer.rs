//! 1 CLI セッション内で (露出, NR, decoder sharpness) の decode を共有する。
use super::{
    metrics::{image_metrics, Metrics},
    stars::ExifHints,
    RgbImage,
};
use crate::coreimage::{
    batch::{is_handle_lost, BatchSession},
    CoreImageError,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

pub const MAX_EDGE: usize = 640;
static NEXT_RUN: AtomicU64 = AtomicU64::new(0);
pub fn error(message: impl Into<String>) -> CoreImageError {
    CoreImageError::new("CANDIDATES_FAILED", message)
}

/// この実行が新規作成した一時ディレクトリだけを回収する（成功・エラー・panic 共通）。
struct Scratch(PathBuf);
impl Scratch {
    fn new(parent: &Path) -> Result<Self, CoreImageError> {
        fs::create_dir_all(parent).map_err(|e| error(e.to_string()))?;
        loop {
            let path = parent.join(format!(
                "pso-{}-{}",
                std::process::id(),
                NEXT_RUN.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Ok(Self(path)),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(error(e.to_string())),
            }
        }
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

pub struct Renderer {
    session: BatchSession,
    scratch: Scratch,
    input: PathBuf,
    handles: HashMap<String, String>,
    pub decodes: usize,
    pub renders: usize,
    pub decode_ms: f64,
    pub render_ms: f64,
}
impl Renderer {
    pub fn new(sidecar: &Path, input: &Path, scratch: &Path) -> Result<Self, CoreImageError> {
        Ok(Self {
            session: BatchSession::start(sidecar, Duration::from_secs(60))?,
            scratch: Scratch::new(scratch)?,
            input: input.to_path_buf(),
            handles: HashMap::new(),
            decodes: 0,
            renders: 0,
            decode_ms: 0.0,
            render_ms: 0.0,
        })
    }
    fn key(a: &Value) -> String {
        // hr と WB はこの実行中固定。min(s,1) も鍵に含めて将来の呼出側変更で誤共有しない。
        json!([
            a["exposure"],
            a["highlight_recovery"],
            a["wb_temp"],
            a["sharpness"].as_f64().unwrap().min(1.0),
            a["noise_reduction"]
        ])
        .to_string()
    }
    fn decode(&mut self, a: &Value) -> Result<String, CoreImageError> {
        let handle = self.session.decode(
            &json!({"input":self.input,"mode":"preview","max_edge":MAX_EDGE,"decoder_version":"9",
            "settings":{"profile":"apple_default","lens_correction":true},"adjustments":a}),
        )?;
        self.decode_ms += handle.response["elapsed_ms"]["total"]
            .as_f64()
            .unwrap_or(0.0);
        // CLI 側の上限変更等による LRU 破棄も反映する。
        if let Some(evicted) = handle.response["cache"]["evicted"].as_array() {
            self.handles
                .retain(|_, h| !evicted.iter().any(|v| v.as_str() == Some(h)));
        }
        self.decodes += 1;
        self.handles.insert(Self::key(a), handle.id.clone());
        Ok(handle.id)
    }
    pub fn render(&mut self, a: &Value) -> Result<RgbImage, CoreImageError> {
        let key = Self::key(a);
        let handle = match self.handles.get(&key) {
            Some(h) => h.clone(),
            None => self.decode(a)?,
        };
        let output = self.scratch.0.join("proxy.jpg");
        let request = json!({"output":output,"format":"jpeg","jpeg_quality":1.0,"adjustments":a});
        let response = match self.session.render(&handle, &request) {
            Err(e) if is_handle_lost(&e) => {
                let handle = self.decode(a)?;
                self.session.render(&handle, &request)?
            }
            result => result?,
        };
        self.render_ms += response["elapsed_ms"]["total"].as_f64().unwrap_or(0.0);
        self.renders += 1;
        let image =
            image::open(&output).map_err(|e| error(format!("代理画像を読めません: {e}")))?;
        RgbImage::from_dynamic(&image).map_err(error)
    }
    pub fn finish(mut self) -> Result<(), CoreImageError> {
        for handle in self.handles.values() {
            self.session.release(handle)?;
        }
        let status = self.session.finish()?;
        if status.success() {
            Ok(())
        } else {
            Err(error("Core Image CLI が異常終了しました"))
        }
    }
}

pub struct Evaluator {
    pub renderer: Renderer,
    pub image: RgbImage,
    pub base_metrics: Metrics,
    exif: ExifHints,
    shadow_mask: Vec<bool>,
    memo: HashMap<String, Metrics>,
}
impl Evaluator {
    pub fn new(
        mut renderer: Renderer,
        base: &Value,
        exif: ExifHints,
    ) -> Result<Self, CoreImageError> {
        let image = renderer.render(base)?;
        let gray = image.gray();
        let mut sorted = gray.clone();
        sorted.sort_unstable_by(f32::total_cmp);
        let p = (sorted.len() - 1) as f64 * 0.25;
        let q = sorted[p.floor() as usize] as f64
            + (sorted[p.ceil() as usize] - sorted[p.floor() as usize]) as f64 * p.fract();
        let shadow_mask = gray.iter().map(|v| *v <= q as f32).collect::<Vec<_>>();
        let base_metrics =
            image_metrics(&image, &image, &exif, false, Some(&shadow_mask)).map_err(error)?;
        Ok(Self {
            renderer,
            image,
            base_metrics,
            exif,
            shadow_mask,
            memo: HashMap::new(),
        })
    }
    pub fn enable_stars(&mut self, base: &Value, gate: bool) -> Result<(), CoreImageError> {
        self.base_metrics = image_metrics(
            &self.image,
            &self.image,
            &self.exif,
            gate,
            Some(&self.shadow_mask),
        )
        .map_err(error)?;
        self.memo
            .insert(json!([base, gate]).to_string(), self.base_metrics.clone());
        Ok(())
    }
    pub fn evaluate(&mut self, a: &Value, gate: bool) -> Result<Metrics, CoreImageError> {
        let key = json!([a, gate]).to_string();
        if let Some(m) = self.memo.get(&key) {
            return Ok(m.clone());
        }
        let transformed = self.renderer.render(a)?;
        let m = image_metrics(
            &self.image,
            &transformed,
            &self.exif,
            gate,
            Some(&self.shadow_mask),
        )
        .map_err(error)?;
        self.memo.insert(key, m.clone());
        Ok(m)
    }
}
