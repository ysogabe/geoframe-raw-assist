use super::{
    image_ops::{components, gaussian},
    metrics::{image_metrics, Metrics},
    pareto::pareto_front,
    selection::{guard_failure, reasons, score},
    stars::{detect_star_scene, star_measurements, ExifHints},
    RgbImage,
};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures")
}
fn json(path: impl AsRef<Path>) -> Value {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}
fn baseline(name: &str) -> Value {
    json(fixtures().join("baseline").join(name))
}
fn metrics(value: &Value) -> Metrics {
    serde_json::from_value(value.clone()).unwrap()
}
fn hints(value: &Value) -> ExifHints {
    serde_json::from_value(value.clone()).unwrap()
}
fn compare(actual: &Value, expected: &Value, errors: &mut BTreeMap<String, f64>) {
    assert_eq!(
        actual.as_object().unwrap().len(),
        expected.as_object().unwrap().len()
    );
    for (key, e) in expected.as_object().unwrap() {
        let a = &actual[key];
        if let Some(v) = e.as_f64() {
            let error = (a.as_f64().unwrap() - v).abs();
            let max = errors.entry(key.clone()).or_default();
            *max = max.max(error);
            let tolerance = if key == "star_count" {
                0.0
            } else if key.contains("clip") || key == "shadow_crushing" {
                1e-4
            } else {
                1e-3
            };
            assert!(error <= tolerance, "{key}: {a} != {e}, error={error}");
        } else {
            assert_eq!(a, e, "{key}");
        }
    }
}
#[test]
fn frozen_synthetic_metrics_and_stars() {
    let data = baseline("metrics.json");
    let mut errors = BTreeMap::new();
    for row in data["images"].as_array().unwrap() {
        let image = image::open(
            fixtures()
                .join("baseline")
                .join(row["input"].as_str().unwrap()),
        )
        .unwrap();
        let source = RgbImage::from_dynamic(&image).unwrap();
        let transformed = RgbImage::new(
            source.width,
            source.height,
            source
                .pixels
                .iter()
                .map(|p| p.map(|v| (v * 1.2_f32).clamp(0.0, 1.0)))
                .collect(),
        )
        .unwrap();
        let exif = hints(&row["exif"]);
        for gate in [false, true] {
            let m = image_metrics(&source, &transformed, &exif, gate, None).unwrap();
            let value = serde_json::to_value(m).unwrap();
            compare(
                &value,
                &row["metrics"][if gate { "True" } else { "False" }],
                &mut errors,
            );
            let s = star_measurements(&source, &exif, gate);
            compare(
                &serde_json::to_value(s).unwrap(),
                &row[if gate {
                    "star_measurements"
                } else {
                    "star_ungated"
                }],
                &mut errors,
            );
        }
    }
    println!("synthetic maximum absolute errors: {errors:?}");
}
#[test]
fn frozen_pareto_and_tolerance() {
    for row in baseline("metrics.json")["pareto"].as_array().unwrap() {
        let points: Vec<Vec<f64>> = serde_json::from_value(row["points"].clone()).unwrap();
        assert_eq!(
            serde_json::to_value(pareto_front(&points)).unwrap(),
            row["indices"]
        );
    }
    assert_eq!(
        pareto_front(&[vec![0.0], vec![0.5e-12], vec![2e-12]]),
        vec![0, 1]
    );
    assert_eq!(pareto_front(&[vec![], vec![]]), vec![0, 1]);
    // NaN は numpy の比較と同様に非劣解に残る。
    assert_eq!(pareto_front(&[vec![f64::NAN], vec![1.0]]), vec![0, 1]);
}
fn check_candidate_result(result: &Value) {
    let candidates = result["candidates"].as_array().unwrap();
    let base = metrics(&candidates[0]["metrics"]);
    for c in candidates {
        let m = metrics(&c["metrics"]);
        let style = c["id"].as_str().unwrap();
        assert_eq!(score(&m, style, &base), c["score"].as_f64().unwrap());
        assert_eq!(
            serde_json::to_value(reasons(
                style,
                &m,
                &base,
                result["star_scene"]["is_star_scene"].as_bool().unwrap()
            ))
            .unwrap(),
            c["reasons"]
        );
        // natural は暗部を明るくする安全策の対象ではない。
        assert_eq!(guard_failure(style, &m, &base), None);
    }
}
#[test]
fn frozen_candidate_scores_reasons_and_guards() {
    for row in baseline("candidates.json")["records"].as_array().unwrap() {
        check_candidate_result(&row["result"]);
    }
    let series = baseline("candidate_series.json");
    check_candidate_result(&series["expected"]);
    let expected = json(fixtures().join("pso-selection.json"));
    let base = metrics(&series["expected"]["candidates"][0]["metrics"]);
    for row in expected["series"].as_array().unwrap() {
        let m = metrics(
            &series["evaluations"][row["evaluation"].as_u64().unwrap() as usize]["metrics"],
        );
        let style = row["style"].as_str().unwrap();
        assert_eq!(
            serde_json::to_value(guard_failure(style, &m, &base)).unwrap(),
            row["guard"]
        );
        assert_eq!(score(&m, style, &base), row["score"].as_f64().unwrap());
        assert_eq!(
            serde_json::to_value(reasons(style, &m, &base, true)).unwrap(),
            row["reasons"]
        );
    }
    for row in expected["edges"].as_array().unwrap() {
        let m = metrics(&row["metrics"]);
        let b = metrics(&row["base"]);
        let style = row["style"].as_str().unwrap();
        assert_eq!(
            serde_json::to_value(guard_failure(style, &m, &b)).unwrap(),
            row["guard"]
        );
        assert_eq!(score(&m, style, &b), row["score"].as_f64().unwrap());
        assert_eq!(
            serde_json::to_value(reasons(style, &m, &b, true)).unwrap(),
            row["reasons"]
        );
    }
}
#[test]
fn image_validation_masks_and_sixteen_bit() {
    assert!(RgbImage::new(0, 1, vec![]).is_err());
    assert!(RgbImage::new(1, 1, vec![[f32::NAN, 0.0, 0.0]]).is_err());
    assert!(RgbImage::new(2, 1, vec![[0.0; 3]]).is_err());
    let buffer = image::ImageBuffer::<image::Rgb<u16>, Vec<u16>>::from_pixel(
        1,
        1,
        image::Rgb([0, 32768, 65535]),
    );
    let source = RgbImage::from_dynamic(&image::DynamicImage::ImageRgb16(buffer)).unwrap();
    assert_eq!(source.pixels[0][0], 0.0);
    assert_eq!(source.pixels[0][2], 1.0);
    assert!((source.pixels[0][1] - 32768.0 / 65535.0).abs() < 1e-7);
    let black = RgbImage::new(1, 1, vec![[0.0; 3]]).unwrap();
    let exif = ExifHints::default();
    assert!(image_metrics(&black, &black, &exif, false, Some(&[])).is_err());
    let m = image_metrics(&source, &source, &exif, false, Some(&[false])).unwrap();
    assert_eq!(
        m.shadow_mean,
        image_metrics(&source, &source, &exif, false, None)
            .unwrap()
            .shadow_mean
    );
    let mask_source = RgbImage::new(2, 1, vec![[0.0; 3], [1.0; 3]]).unwrap();
    assert_eq!(
        image_metrics(
            &mask_source,
            &mask_source,
            &exif,
            false,
            Some(&[true, false])
        )
        .unwrap()
        .shadow_mean,
        0.0
    );
}
#[test]
fn connected_components_are_eight_connected_and_blur_reflects() {
    let c = components(
        &[true, false, false, false, true, false, false, false, true],
        3,
        3,
    );
    assert_eq!(c.len(), 1);
    assert_eq!(
        (c[0].x, c[0].y, c[0].width, c[0].height, c[0].area),
        (0, 0, 3, 3, 3)
    );
    assert!(components(&[false; 9], 3, 3).is_empty());
    for (w, h) in [(1, 1), (1, 3), (3, 1)] {
        for v in gaussian(&vec![0.5; w * h], w, h, 3.0) {
            assert!((v - 0.5).abs() < 1e-6);
        }
    }
}
#[test]
fn star_exif_gate_and_unknown_fallback() {
    // unknown 経路には局所ピークの面積が 4px 以下の点を使う。
    // baseline/stars.png の 3x3 の星は、この経路では面積上限で除外される。
    let mut pixels = vec![[0.02; 3]; 96 * 96];
    for y in (8..90).step_by(13) {
        for x in (7..90).step_by(17) {
            pixels[y * 96 + x] = [0.9; 3];
        }
    }
    let image = RgbImage::new(96, 96, pixels).unwrap();
    assert!(detect_star_scene(&image, &ExifHints::default()).is_star_scene);
    let day = detect_star_scene(
        &image,
        &ExifHints {
            exposure_time: Some(0.1),
            ..Default::default()
        },
    );
    assert!(!day.is_star_scene);
    assert_eq!(day.exif_state, "daylight");
    assert_eq!(day.star_point_density, None);
    for t in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        assert_eq!(
            detect_star_scene(
                &image,
                &ExifHints {
                    exposure_time: Some(t),
                    ..Default::default()
                }
            )
            .exif_state,
            "unknown"
        );
    }
    let night = ExifHints {
        exposure_time: Some(2.0),
        f_number: Some(1.0),
        iso: Some(200.0),
    };
    assert_eq!(detect_star_scene(&image, &night).exif_state, "night"); // EV100 = -2
    let day = ExifHints {
        iso: Some(199.0),
        ..night
    };
    assert_eq!(detect_star_scene(&image, &day).exif_state, "daylight");
    assert!(!star_measurements(&image, &night, false).is_star_scene);
}

#[test]
#[ignore = "実機写真由来の入力。先に scripts/freeze_pso_parity.py --real-images を実行する"]
fn real_eleven_star_gates_and_metrics_match_python() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../work/pso-parity");
    let records = json(dir.join("reference.json"));
    assert_eq!(records.as_array().unwrap().len(), 11);
    let mut errors = BTreeMap::new();
    let mut matches = 0;
    let frozen = baseline("candidates.json");
    for row in records.as_array().unwrap() {
        let capture = row["capture_id"].as_str().unwrap();
        let bytes = std::fs::read(dir.join(format!("{capture}.f32"))).unwrap();
        let pixels = bytes
            .chunks_exact(12)
            .map(|p| {
                std::array::from_fn(|c| f32::from_le_bytes(p[c * 4..c * 4 + 4].try_into().unwrap()))
            })
            .collect();
        let image = RgbImage::new(
            row["width"].as_u64().unwrap() as usize,
            row["height"].as_u64().unwrap() as usize,
            pixels,
        )
        .unwrap();
        let mask: Vec<bool> = std::fs::read(dir.join(format!("{capture}.mask")))
            .unwrap()
            .into_iter()
            .map(|v| v != 0)
            .collect();
        let exif = hints(&row["exif"]);
        let fixed = frozen["records"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["case"]["capture_id"] == row["capture_id"])
            .unwrap();
        let star = detect_star_scene(&image, &exif);
        compare(
            &serde_json::to_value(&star).unwrap(),
            &fixed["result"]["star_scene"],
            &mut errors,
        );
        assert_eq!(
            star.is_star_scene,
            row["frozen_gate"].as_bool().unwrap(),
            "{capture}"
        );
        compare(
            &serde_json::to_value(star).unwrap(),
            &row["star"],
            &mut errors,
        );
        matches += 1;
        for gate in [false, true] {
            let m = image_metrics(&image, &image, &exif, gate, Some(&mask)).unwrap();
            let value = serde_json::to_value(m).unwrap();
            compare(
                &value,
                &row["metrics"][if gate { "True" } else { "False" }],
                &mut errors,
            );
            if gate == row["frozen_gate"].as_bool().unwrap() {
                compare(
                    &value,
                    &fixed["result"]["candidates"][0]["metrics"],
                    &mut errors,
                );
            }
        }
    }
    println!("star gates: {matches}/11; real maximum absolute errors: {errors:?}");
}
