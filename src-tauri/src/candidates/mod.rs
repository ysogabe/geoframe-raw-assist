//! PSO の純関数。探索ループと Tauri コマンドは #87 で接続する。
#![allow(dead_code)] // #87 で使うため、接続前の純関数を保持する。

mod image_ops;
pub mod metrics;
pub mod pareto;
pub mod selection;
pub mod stars;

/// 行優先の sRGB float32。構築時に有限値・寸法を検証し 0..1 に正規化する。
#[derive(Clone, Debug)]
pub struct RgbImage {
    pub width: usize,
    pub height: usize,
    pub pixels: Vec<[f32; 3]>,
}
impl RgbImage {
    pub fn new(width: usize, height: usize, mut pixels: Vec<[f32; 3]>) -> Result<Self, String> {
        if width == 0 || height == 0 || width.checked_mul(height) != Some(pixels.len()) {
            return Err("代理画像の寸法が不正です。".into());
        }
        if pixels.iter().flatten().any(|v| !v.is_finite()) {
            return Err("代理画像に不正な画素値があります。".into());
        }
        let scale = if pixels.iter().flatten().any(|v| *v > 1.0) {
            255.0
        } else {
            1.0
        };
        for v in pixels.iter_mut().flatten() {
            *v = (*v / scale).clamp(0.0, 1.0);
        }
        Ok(Self {
            width,
            height,
            pixels,
        })
    }
    /// 8/16bit の両方を image crate の変換で 0..1 にする。
    pub fn from_dynamic(image: &image::DynamicImage) -> Result<Self, String> {
        let rgb = image.to_rgb32f();
        Self::new(
            rgb.width() as usize,
            rgb.height() as usize,
            rgb.pixels().map(|p| p.0).collect(),
        )
    }
    pub fn gray(&self) -> Vec<f32> {
        self.pixels
            .iter()
            .map(|p| p[0] * 0.2126 + p[1] * 0.7152 + p[2] * 0.0722)
            .collect()
    }
}

#[cfg(test)]
mod tests;
