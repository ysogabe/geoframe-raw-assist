//! OpenCV CV_32F GaussianBlur: 自動カーネル幅 (sigma * 8 + 1)、BORDER_REFLECT_101。
pub fn round6(v: f64) -> f64 {
    (v * 1e6).round_ties_even() / 1e6
}
pub fn mean(v: &[f32]) -> f64 {
    v.iter().map(|x| *x as f64).sum::<f64>() / v.len() as f64
}
pub fn std(v: &[f32]) -> f64 {
    let m = mean(v);
    (v.iter().map(|x| (*x as f64 - m).powi(2)).sum::<f64>() / v.len() as f64).sqrt()
}
fn reflect(mut p: isize, n: usize) -> usize {
    if n == 1 {
        return 0;
    }
    while p < 0 || p >= n as isize {
        p = if p < 0 { -p } else { 2 * n as isize - p - 2 };
    }
    p as usize
}
pub fn gaussian(values: &[f32], w: usize, h: usize, sigma: f64) -> Vec<f32> {
    let size = ((sigma * 8.0 + 1.0).round_ties_even() as usize) | 1;
    let radius = size / 2;
    let mut kernel: Vec<f64> = (0..size)
        .map(|i| (-0.5 * ((i as f64 - radius as f64) / sigma).powi(2)).exp())
        .collect();
    let sum: f64 = kernel.iter().sum();
    for k in &mut kernel {
        *k /= sum;
    }
    let kernel: Vec<f32> = kernel.into_iter().map(|k| k as f32).collect();
    let mut tmp = vec![0.0; values.len()];
    let mut out = tmp.clone();
    for y in 0..h {
        for x in 0..w {
            tmp[y * w + x] = kernel
                .iter()
                .enumerate()
                .map(|(i, k)| {
                    values[y * w + reflect(x as isize + i as isize - radius as isize, w)] * k
                })
                .sum();
        }
    }
    for y in 0..h {
        for x in 0..w {
            out[y * w + x] = kernel
                .iter()
                .enumerate()
                .map(|(i, k)| {
                    tmp[reflect(y as isize + i as isize - radius as isize, h) * w + x] * k
                })
                .sum();
        }
    }
    out
}
#[derive(Debug)]
pub struct Component {
    pub x: usize,
    pub y: usize,
    pub width: usize,
    pub height: usize,
    pub area: usize,
}
/// Python の非 cv2 フォールバックと同じ 8 近傍 flood fill。
pub fn components(mask: &[bool], w: usize, h: usize) -> Vec<Component> {
    let mut seen = vec![false; mask.len()];
    let mut result = Vec::new();
    for start in 0..mask.len() {
        if !mask[start] || seen[start] {
            continue;
        }
        seen[start] = true;
        let mut stack = vec![start];
        let (mut left, mut right, mut top, mut bottom, mut area) = (w, 0, h, 0, 0);
        while let Some(i) = stack.pop() {
            let (x, y) = (i % w, i / w);
            left = left.min(x);
            right = right.max(x);
            top = top.min(y);
            bottom = bottom.max(y);
            area += 1;
            for ny in y.saturating_sub(1)..=(y + 1).min(h - 1) {
                for nx in x.saturating_sub(1)..=(x + 1).min(w - 1) {
                    let j = ny * w + nx;
                    if mask[j] && !seen[j] {
                        seen[j] = true;
                        stack.push(j);
                    }
                }
            }
        }
        result.push(Component {
            x: left,
            y: top,
            width: right - left + 1,
            height: bottom - top + 1,
            area,
        });
    }
    result
}
