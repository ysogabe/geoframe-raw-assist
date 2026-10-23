//! numpy の乱数列は再現しない。SHA-256 seed + SplitMix64 / Box-Muller で系列を固定する。
use super::{metrics::Metrics, pareto::pareto_front, selection::guard_failure};
use crate::coreimage::CoreImageError;
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const KEYS: [&str; 6] = [
    "exposure",
    "shadow_lift",
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
];
const LOW: [f64; 6] = [-2.0, 0.0, 0.7, 0.8, 0.0, 0.8];
const HIGH: [f64; 6] = [2.0, 100.0, 1.4, 1.4, 1.5, 1.6];
// CI は増分 USM: min(1.2, 0.7 * (s-1) * 1.5)。原寸の飽和点は
// 1 + 1.2/(0.7*1.5) = 2.142857 > 契約上限 1.6。旧 rawpy の 1.47 は使わない。
const SHARPNESS_SEARCH_MAX: f64 = 1.6;
pub type Vector = [f64; 6];

pub fn default_seed(capture: &str, preset: &str) -> u32 {
    let digest = Sha256::digest(format!("{capture}:{preset}"));
    u32::from_be_bytes(digest[..4].try_into().unwrap())
}
fn style_seed(seed: u32, style: &str) -> u32 {
    let digest = Sha256::digest(format!("{seed}:{style}"));
    u32::from_le_bytes(digest[..4].try_into().unwrap())
}
struct Rng(u64);
impl Rng {
    fn uniform(&mut self) -> f64 {
        self.0 = self.0.wrapping_add(0x9e3779b97f4a7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
        z ^= z >> 31;
        ((z >> 11) as f64 + 0.5) / ((1u64 << 53) as f64)
    }
    fn normal(&mut self) -> f64 {
        (-2.0 * self.uniform().ln()).sqrt() * (std::f64::consts::TAU * self.uniform()).cos()
    }
}
pub fn vector(a: &Value) -> Vector {
    std::array::from_fn(|i| a[KEYS[i]].as_f64().expect("normalized adjustments"))
}
pub fn target(base: Vector, style: &str) -> Vector {
    let offset = match style {
        "stars" => [0.08, 8.0, 0.01, -0.02, -0.02, -0.02],
        "foreground" => [0.16, 25.0, 0.06, -0.05, 0.02, 0.01],
        "low_noise" => [0.05, 15.0, 0.03, -0.04, 0.0, -0.09],
        "social" => [0.03, -3.0, 0.01, 0.12, 0.12, 0.09],
        _ => [0.0; 6],
    };
    std::array::from_fn(|i| (base[i] + offset[i]).clamp(LOW[i], HIGH[i]))
}
/// プリセット露出と ±0.5EV の3格子。後段 brightness で RAW 露出を近似しない。
pub fn exposure_grid(base: f64) -> Vec<f64> {
    let mut grid = vec![
        (base - 0.5).clamp(-1.5, 1.5),
        base,
        (base + 0.5).clamp(-1.5, 1.5),
    ];
    grid.sort_by(f64::total_cmp);
    grid.dedup();
    grid
}
/// 固定 NR 表。natural は呼出側でプリセットをそのまま使う。
pub fn noise_reduction(style: &str) -> &'static str {
    match style {
        "low_noise" => "full",
        _ => "light",
    }
}
pub fn adjustments(v: Vector, base: &Value, nr: &str) -> Value {
    let mut a = base.clone();
    let grid = exposure_grid(base["exposure"].as_f64().unwrap());
    for i in 0..6 {
        let value = if i == 0 {
            *grid
                .iter()
                .min_by(|a, b| (**a - v[i]).abs().total_cmp(&(**b - v[i]).abs()))
                .unwrap()
        } else if i == 5 {
            // s<1 は decode のキー。soft プリセットでは固定し、それ以外は後段側だけ探索。
            if base["sharpness"].as_f64().unwrap() < 1.0 {
                base["sharpness"].as_f64().unwrap()
            } else {
                v[i].clamp(1.0, SHARPNESS_SEARCH_MAX)
            }
        } else {
            v[i].clamp(LOW[i], HIGH[i])
        };
        let digits = if i == 1 { 10.0 } else { 10000.0 };
        a[KEYS[i]] = Value::from((value * digits).round() / digits);
    }
    a["noise_reduction"] = Value::from(nr);
    a
}
pub fn safe(v: Vector) -> bool {
    v[0].abs() <= 1.85
        && v[4] <= 1.42
        && v[5] <= SHARPNESS_SEARCH_MAX
        && !(v[1] > 96.0 && v[2] > 1.30)
        && !(v[3] > 1.37 && v[2] < 0.76)
}
pub fn distance(a: Vector, b: Vector) -> f64 {
    (a.iter()
        .zip(b)
        .enumerate()
        .map(|(i, (a, b))| ((a - b) / (HIGH[i] - LOW[i])).powi(2))
        .sum::<f64>()
        / 6.0)
        .sqrt()
}
fn guard_penalty(style: &str, m: &Metrics, base: &Metrics) -> f64 {
    let mut p = (m.highlight_clip - base.highlight_clip - 0.05).max(0.0)
        + (m.shadow_clip - base.shadow_clip - 0.05).max(0.0);
    if style == "low_noise" {
        p += (m.noise_flat - base.noise_flat).max(0.0) * 10.0;
    }
    if style == "foreground" {
        p += (base.shadow_mean + 0.005 - m.shadow_mean).max(0.0) * 2.0;
    }
    p
}
pub fn objectives(
    m: &Metrics,
    style: &str,
    v: Vector,
    target: Vector,
    base_v: Vector,
    reference: usize,
    penalty: f64,
) -> Vec<f64> {
    vec![
        m.highlight_clip * 1.9,
        m.shadow_clip * 1.5,
        m.noise_flat * if style == "low_noise" { 1.25 } else { 0.75 },
        (0.72 - m.detail).max(0.0) * 1.2,
        m.halo * if style != "social" { 1.35 } else { 1.0 },
        m.color_saturation_clip * if style != "social" { 1.3 } else { 0.9 },
        (0.68 - m.tonal_range).max(0.0),
        m.source_deviation * 0.55,
        distance(v, target) * 0.65,
        distance(v, base_v) * 0.1,
        if style == "stars" {
            (reference as f64 - m.star_count as f64).max(0.0) / reference.max(1) as f64 * 2.2
        } else {
            0.0
        },
        penalty * 4.0,
    ]
}
fn utility(o: &[f64], style: &str) -> f64 {
    let weights = [
        1.2,
        1.0,
        1.1,
        0.75,
        0.9,
        0.9,
        0.75,
        1.1,
        1.0,
        0.3,
        if style == "stars" { 1.5 } else { 0.0 },
        3.0,
    ];
    o.iter().zip(weights).map(|(v, w)| v * w).sum()
}
#[derive(Clone)]
pub struct Evaluation {
    pub adjustments: Value,
    pub metrics: Metrics,
    pub failure: Option<String>,
}
#[derive(Clone)]
struct Point {
    v: Vector,
    evaluation: Evaluation,
    objectives: Vec<f64>,
}
fn dominates(a: &[f64], b: &[f64]) -> bool {
    a.iter().zip(b).all(|(a, b)| *a <= *b + 1e-12) && a.iter().zip(b).any(|(a, b)| *a < *b - 1e-12)
}
fn select(points: &[Point], style: &str) -> usize {
    let front = pareto_front(
        &points
            .iter()
            .map(|p| p.objectives.clone())
            .collect::<Vec<_>>(),
    );
    // 技術的な値域だけでなく safe_vector を最終候補にも必ず適用する。
    let eligible = |i: &usize| safe(points[*i].v) && points[*i].evaluation.failure.is_none();
    let passing: Vec<_> = front.iter().copied().filter(eligible).collect();
    let passing = if passing.is_empty() {
        (0..points.len()).filter(eligible).collect()
    } else {
        passing
    };
    let pool = if passing.is_empty() {
        (0..points.len())
            .filter(|i| safe(points[*i].v))
            .collect::<Vec<_>>()
    } else {
        passing
    };
    *pool
        .iter()
        .min_by(|a, b| {
            utility(&points[**a].objectives, style)
                .total_cmp(&utility(&points[**b].objectives, style))
        })
        .unwrap_or(&0)
}
pub fn optimise(
    base: &Value,
    base_metrics: &Metrics,
    style: &str,
    budget: usize,
    seed: u32,
    mut evaluate: impl FnMut(&Value, bool) -> Result<Metrics, CoreImageError>,
) -> Result<Evaluation, CoreImageError> {
    let base_v = vector(base);
    let wanted = target(base_v, style);
    let particles = 8.min(4.max(budget / 2));
    let generations = (budget / particles).max(1);
    let mut rng = Rng(style_seed(seed, style) as u64);
    let mut positions: Vec<Vector> = (0..particles)
        .map(|_| std::array::from_fn(|i| wanted[i] + rng.normal() * 0.09 * (HIGH[i] - LOW[i])))
        .collect();
    positions[0] = base_v;
    positions[1] = wanted;
    // 3露出を初代で巡回。後の世代も同じ格子へ写像してデコード数を制限する。
    let grid = exposure_grid(base_v[0]);
    for (i, p) in positions.iter_mut().enumerate().skip(2) {
        p[0] = grid[(i - 2) % grid.len()];
    }
    let mut velocities: Vec<Vector> = (0..particles)
        .map(|_| std::array::from_fn(|i| rng.normal() * 0.025 * (HIGH[i] - LOW[i])))
        .collect();
    let mut best: Vec<Option<Point>> = vec![None; particles];
    let mut all = Vec::new();
    for _ in 0..generations {
        let mut current = Vec::new();
        for position in &positions {
            let a = adjustments(*position, base, noise_reduction(style));
            let v = vector(&a); // 採用するスナップ後の値で採点・安全判定。
            let m = evaluate(&a, style == "stars")?;
            let mut o = objectives(
                &m,
                style,
                v,
                wanted,
                base_v,
                base_metrics.star_count,
                guard_penalty(style, &m, base_metrics),
            );
            if !safe(v) {
                for value in &mut o {
                    *value += 3.0;
                }
            }
            let mut failure = guard_failure(style, &m, base_metrics);
            // star_loss に加えて最終選択も星数減少を防ぐ（#87 完了条件）。
            if style == "stars" && m.star_count < base_metrics.star_count {
                failure = Some("星の検出数が基準より減るため除外".into());
            }
            if !safe(v) {
                failure = Some("極端な調整値のため除外".into());
            }
            current.push(Point {
                v,
                evaluation: Evaluation {
                    adjustments: a,
                    metrics: m,
                    failure,
                },
                objectives: o,
            });
        }
        for (i, p) in current.iter().enumerate() {
            if best[i].as_ref().map_or(true, |old| {
                dominates(&p.objectives, &old.objectives)
                    || (!dominates(&old.objectives, &p.objectives)
                        && utility(&p.objectives, style) < utility(&old.objectives, style))
            }) {
                best[i] = Some(p.clone());
            }
        }
        let front = pareto_front(
            &current
                .iter()
                .map(|p| p.objectives.clone())
                .collect::<Vec<_>>(),
        );
        let leader = current[*front
            .iter()
            .min_by(|a, b| {
                utility(&current[**a].objectives, style)
                    .total_cmp(&utility(&current[**b].objectives, style))
            })
            .unwrap()]
        .v;
        for i in 0..particles {
            for (d, leader_value) in leader.iter().enumerate() {
                velocities[i][d] = 0.56 * velocities[i][d]
                    + 1.25 * rng.uniform() * (best[i].as_ref().unwrap().v[d] - positions[i][d])
                    + 1.40 * rng.uniform() * (leader_value - positions[i][d]);
                positions[i][d] = (positions[i][d] + velocities[i][d]).clamp(LOW[d], HIGH[d]);
            }
        }
        all.extend(current);
    }
    Ok(all[select(&all, style)].evaluation.clone())
}

#[cfg(test)]
mod replay_tests {
    use super::*;
    #[test]
    fn frozen_series_selects_same_pareto_candidates() {
        let series: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/baseline/candidate_series.json"
        ))
        .unwrap();
        let expected = series["expected"]["candidates"].as_array().unwrap();
        let base: Metrics = serde_json::from_value(expected[0]["metrics"].clone()).unwrap();
        let inputs = series["objective_inputs"].as_array().unwrap();
        let evaluations = series["evaluations"].as_array().unwrap();
        assert_eq!(inputs.len(), evaluations.len());
        for style in ["foreground", "low_noise", "social"] {
            let points: Vec<_> = inputs
                .iter()
                .zip(evaluations)
                .filter(|(i, _)| i["style"] == style)
                .map(|(i, e)| {
                    let m: Metrics = serde_json::from_value(e["metrics"].clone()).unwrap();
                    let mut o: Vec<f64> =
                        serde_json::from_value(i["objectives_before_safety_penalty"].clone())
                            .unwrap();
                    if i["safe_vector"] == false {
                        for v in &mut o {
                            *v += 3.0;
                        }
                    }
                    Point {
                        v: vector(&e["adjustments"]),
                        evaluation: Evaluation {
                            adjustments: e["adjustments"].clone(),
                            failure: guard_failure(style, &m, &base),
                            metrics: m,
                        },
                        objectives: o,
                    }
                })
                .collect();
            let picked = &points[select(&points, style)].evaluation;
            let wanted = expected.iter().find(|c| c["id"] == style).unwrap();
            assert_eq!(picked.adjustments, wanted["adjustments"], "{style}");
        }
    }
}
