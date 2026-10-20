//! Adjustments（現像パラメータ 9 キー）の検証と正規化（Issue #72 / #73）。
//!
//! 値域と規則は `local_raw/presets.py`（`ADJUSTMENT_RANGES` / `resolve_adjustments`）と
//! 契約 `docs/API_CONTRACT.md` §2 に合わせる。設計書
//! `docs/design/coreimage-preview-adjustments.md` §4.3。
//!
//! - 9 キーすべて必須。欠けたキー・未知のキーは `ADJUSTMENTS_INVALID`。
//! - 数値は契約の値域へ**クランプ**し、`f64` に正規化する（`15` と `15.0`、`-0.0` と `0.0` で
//!   キャッシュ鍵が割れないように）。bool・文字列・null・非有限値は拒否する。
//! - `noise_reduction` は `off` / `light` / `full` のいずれか（大文字小文字は区別する）。
//!
//! 今後の拡張（#80 プリセットの Rust 移行）: このモジュールにプリセット表と
//! `resolve_adjustments(preset, overrides)` を足し、最後に `normalize_adjustments` を通す形にする。
//! Tauri コマンドの署名は変えない。正規化規則（クランプ・float 化）を変えたら
//! `coreimage::CACHE_KEY_SCHEMA` を上げること。

use serde_json::{Map, Value};

use crate::coreimage::CoreImageError;

/// 検証エラーのコード（フロントはフォールバックせずバグとして表示する。設計書 §2.6）。
pub const ADJUSTMENTS_INVALID: &str = "ADJUSTMENTS_INVALID";

/// 数値キーの値域（契約 §2。`local_raw/presets.py: ADJUSTMENT_RANGES` と同じ順・同じ値）。
pub const ADJUSTMENT_RANGES: [(&str, f64, f64); 8] = [
    ("exposure", -2.0, 2.0),
    ("wb_temp", -100.0, 100.0),
    ("shadow_lift", 0.0, 100.0),
    ("highlight_recovery", 0.0, 100.0),
    ("brightness", 0.7, 1.4),
    ("contrast", 0.8, 1.4),
    ("saturation", 0.0, 1.5),
    ("sharpness", 0.8, 1.6),
];

/// 列挙キー。
pub const NOISE_REDUCTION_KEY: &str = "noise_reduction";

/// `noise_reduction` の選択肢（`local_raw/presets.py: NOISE_REDUCTION_CHOICES`）。
pub const NOISE_REDUCTION_CHOICES: [&str; 3] = ["off", "light", "full"];

/// 契約にあるキー（数値 8 種 + `noise_reduction`）か。
fn is_known_key(key: &str) -> bool {
    key == NOISE_REDUCTION_KEY || ADJUSTMENT_RANGES.iter().any(|(name, _, _)| *name == key)
}

fn invalid(message: impl Into<String>) -> CoreImageError {
    CoreImageError::new(ADJUSTMENTS_INVALID, message)
}

/// 数値 1 つを検証してクランプする（`presets.py: _coerce_number` と同じ規則）。
fn coerce_number(
    key: &str,
    value: &Value,
    minimum: f64,
    maximum: f64,
) -> Result<f64, CoreImageError> {
    // serde_json の bool / 文字列は Number ではないので as_f64 が None になる（True が 1.0 に
    // 化けない）。
    let Some(number) = value.as_f64().filter(|_| value.is_number()) else {
        return Err(invalid(format!("{key} には数値を指定してください")));
    };
    if !number.is_finite() {
        return Err(invalid(format!("{key} には有限の数値を指定してください")));
    }
    // `+ 0.0` で -0.0 を 0.0 に寄せる（"-0.0" と "0.0" で鍵が割れないように）。
    Ok(number.clamp(minimum, maximum) + 0.0)
}

/// Adjustments 全量を検証・正規化する。
///
/// 返り値は契約の並び順で組み直した新しいオブジェクト（入力のキー順や int/float の違いに
/// 依存しない）。
pub fn normalize_adjustments(value: &Value) -> Result<Value, CoreImageError> {
    let Some(input) = value.as_object() else {
        return Err(invalid("adjustments はオブジェクトで指定してください"));
    };

    let mut unknown: Vec<&str> = input
        .keys()
        .map(String::as_str)
        .filter(|key| !is_known_key(key))
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        // キー名は外部入力なので Debug 表記（引用符・エスケープ付き）で出す。
        return Err(invalid(format!("未対応の調整項目です: {unknown:?}")));
    }
    let missing: Vec<&str> = ADJUSTMENT_RANGES
        .iter()
        .map(|(name, _, _)| *name)
        .chain(std::iter::once(NOISE_REDUCTION_KEY))
        .filter(|key| !input.contains_key(*key))
        .collect();
    if !missing.is_empty() {
        return Err(invalid(format!(
            "調整項目が足りません（9 キーすべて必須）: {}",
            missing.join(", ")
        )));
    }

    let mut normalized = Map::new();
    for (key, minimum, maximum) in ADJUSTMENT_RANGES {
        let number = coerce_number(key, &input[key], minimum, maximum)?;
        normalized.insert(key.to_string(), Value::from(number));
    }
    match input[NOISE_REDUCTION_KEY].as_str() {
        Some(choice) if NOISE_REDUCTION_CHOICES.contains(&choice) => {
            normalized.insert(NOISE_REDUCTION_KEY.to_string(), Value::from(choice));
        }
        _ => {
            return Err(invalid(
                "noise_reduction は off / light / full のいずれかで指定してください",
            ));
        }
    }
    Ok(Value::Object(normalized))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn base() -> Value {
        json!({
            "exposure": 0.0,
            "wb_temp": 0.0,
            "shadow_lift": 15.0,
            "highlight_recovery": 60.0,
            "brightness": 1.0,
            "contrast": 1.03,
            "saturation": 1.02,
            "sharpness": 1.04,
            "noise_reduction": "light"
        })
    }

    fn with(key: &str, value: Value) -> Value {
        let mut adjustments = base();
        adjustments[key] = value;
        adjustments
    }

    fn without(key: &str) -> Value {
        let mut adjustments = base();
        adjustments.as_object_mut().unwrap().remove(key);
        adjustments
    }

    fn code(value: &Value) -> String {
        normalize_adjustments(value).unwrap_err().code
    }

    #[test]
    fn accepts_full_valid_set_unchanged() {
        assert_eq!(normalize_adjustments(&base()).unwrap(), base());
    }

    #[test]
    fn integers_become_floats() {
        let normalized = normalize_adjustments(&with("shadow_lift", json!(15))).unwrap();
        assert!(normalized["shadow_lift"].is_f64());
        assert_eq!(
            normalized.to_string(),
            normalize_adjustments(&base()).unwrap().to_string(),
            "15 と 15.0 は同じ表現になる"
        );
    }

    #[test]
    fn negative_zero_is_folded() {
        let normalized = normalize_adjustments(&with("exposure", json!(-0.0))).unwrap();
        assert_eq!(normalized["exposure"].to_string(), "0.0");
    }

    #[test]
    fn clamps_every_numeric_key_to_contract_range() {
        for (key, minimum, maximum) in ADJUSTMENT_RANGES {
            let low = normalize_adjustments(&with(key, json!(minimum - 1000.0))).unwrap();
            assert_eq!(low[key].as_f64(), Some(minimum), "{key} の下限");
            let high = normalize_adjustments(&with(key, json!(maximum + 1000.0))).unwrap();
            assert_eq!(high[key].as_f64(), Some(maximum), "{key} の上限");
            let inside = (minimum + maximum) / 2.0;
            let mid = normalize_adjustments(&with(key, json!(inside))).unwrap();
            assert_eq!(mid[key].as_f64(), Some(inside), "{key} の範囲内はそのまま");
        }
        // 具体例: sharpness 0.5 → 0.8、exposure 3 → 2.0、saturation -1 → 0.0。
        let n = normalize_adjustments(&with("sharpness", json!(0.5))).unwrap();
        assert_eq!(n["sharpness"], json!(0.8));
        let n = normalize_adjustments(&with("exposure", json!(3))).unwrap();
        assert_eq!(n["exposure"], json!(2.0));
        let n = normalize_adjustments(&with("saturation", json!(-1))).unwrap();
        assert_eq!(n["saturation"], json!(0.0));
    }

    #[test]
    fn highlight_recovery_is_kept_not_dropped() {
        let normalized = normalize_adjustments(&base()).unwrap();
        assert_eq!(normalized["highlight_recovery"], json!(60.0));
    }

    #[test]
    fn rejects_non_numbers() {
        for bad in [
            json!(true),
            json!("1.0"),
            Value::Null,
            json!([1.0]),
            json!({"v": 1}),
        ] {
            assert_eq!(
                code(&with("brightness", bad.clone())),
                ADJUSTMENTS_INVALID,
                "{bad}"
            );
        }
    }

    #[test]
    fn rejects_bad_noise_reduction() {
        for bad in [
            json!("Light"),
            json!("strong"),
            json!(""),
            json!(1),
            Value::Null,
            json!(true),
        ] {
            assert_eq!(
                code(&with("noise_reduction", bad.clone())),
                ADJUSTMENTS_INVALID,
                "{bad}"
            );
        }
        for ok in NOISE_REDUCTION_CHOICES {
            let normalized = normalize_adjustments(&with("noise_reduction", json!(ok))).unwrap();
            assert_eq!(normalized["noise_reduction"], json!(ok));
        }
    }

    #[test]
    fn rejects_missing_keys() {
        for (key, _, _) in ADJUSTMENT_RANGES {
            assert_eq!(code(&without(key)), ADJUSTMENTS_INVALID, "{key} 欠落");
        }
        assert_eq!(code(&without("noise_reduction")), ADJUSTMENTS_INVALID);
        assert_eq!(code(&json!({})), ADJUSTMENTS_INVALID);
    }

    #[test]
    fn rejects_unknown_keys() {
        let mut adjustments = base();
        adjustments["../../etc/passwd"] = json!(1.0);
        let error = normalize_adjustments(&adjustments).unwrap_err();
        assert_eq!(error.code, ADJUSTMENTS_INVALID);
        assert!(error.message.contains("\"../../etc/passwd\""));
        let mut adjustments = base();
        adjustments["Exposure"] = json!(0.0);
        assert_eq!(
            code(&adjustments),
            ADJUSTMENTS_INVALID,
            "大文字小文字は区別する"
        );
    }

    #[test]
    fn rejects_non_object() {
        for bad in [Value::Null, json!([]), json!("x"), json!(1)] {
            assert_eq!(code(&bad), ADJUSTMENTS_INVALID, "{bad}");
        }
    }

    #[test]
    fn output_is_independent_of_input_key_order() {
        let reversed: Map<String, Value> = base()
            .as_object()
            .unwrap()
            .iter()
            .rev()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        assert_eq!(
            normalize_adjustments(&Value::Object(reversed))
                .unwrap()
                .to_string(),
            normalize_adjustments(&base()).unwrap().to_string()
        );
    }
}
