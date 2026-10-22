//! プリセット 11 種の定義表と Adjustments の解決（Issue #80）。
//!
//! 定義の正はこのモジュール（Core Image 向けの値）。数値は #81 で `apple_default`・decoder 9・
//! レンズ補正 ON 向けに再調整した（設計書 `docs/design/coreimage-preset-retune.md` §4・§7.2）。
//! `local_raw/presets.py` は rawpy（本現像・書き出し）向けの旧値のまま残す（移行完了 #90 まで。
//! プレビューと書き出しの値の差はオーナー判断で受容、設計書 §11）。
//!
//! フィクスチャは 2 本（設計書 §6）:
//! - `tests/fixtures/presets.json` … **この表の写し**（CI 値）。下のテストが `Value` で突き合わせ、
//!   フロントの `src/lib/format.test.ts`（値域）とハーネス `scripts/compare_engines.py --presets` が読む。
//!   再生成: `UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored`（src-tauri で）。
//! - `tests/fixtures/presets_legacy_rawpy.json`（リポジトリ直下）… Python の旧値の写し。名前・並び・キー構成・
//!   値域の一致は Python の `tests/test_presets_fixture.py` が 2 本を突き合わせて守る（数値は違ってよい）。
//! 契約は `docs/API_CONTRACT.md` §2（型と値域）・§5（プリセット表）・§9（resolve の規則）。
//! 設計の背景は `docs/design/coreimage-feature-migration.md` §3.1。
//!
//! 責務の分担:
//! - このモジュール … プリセット表、`PresetInfo`（`get_presets` の戻り値）、
//!   `resolve_adjustments(preset, overrides)`（既定値へのマージ）。
//! - `crate::adjustments` … Adjustments の値域（`ADJUSTMENT_RANGES` / `NOISE_REDUCTION_CHOICES`）と
//!   検証・正規化（`normalize_adjustments`。未知キー拒否・クランプ・float 化）。
//!   `resolve_adjustments` は最後に必ず `normalize_adjustments` を通すので、規則は 1 か所にしかない。
//!
//! `highlight_recovery`（hr）は CLI 0.6.0 で `exposure` / `shadow_lift` への区分線形の写像として**加算**される
//! （設計書 §2・§3）。非救済の 8 プリセットは hr 0（= CI 既定 + 後段の個性）、hr > 0 は
//! `highlight_rescue` / `backlit_rescue` / `sunset_glow` だけ（案 B、オーナー判断 2026-10-22）。
//! 値は設計書 §7.2 の初期案で、ハーネスの計測（§7.3〜7.5）は未実施（サンプルのボリューム未マウント、
//! `docs/coreimage-preset-retune-report.md`）。

use serde::Serialize;
use serde_json::{Map, Value};

use crate::adjustments::{
    normalize_adjustments, ADJUSTMENTS_INVALID, ADJUSTMENT_RANGES, NOISE_REDUCTION_KEY,
};
use crate::coreimage::CoreImageError;

/// 未知のプリセット名（`presets.py: resolve_adjustments` の「未対応のプリセットです」に当たる）。
pub const UNKNOWN_PRESET: &str = "UNKNOWN_PRESET";

/// 既定のプリセット（`presets.py: DEFAULT_PRESET`）。
#[allow(dead_code)]
pub const DEFAULT_PRESET: &str = "neutral_recovery";

/// プリセット 1 件の定義。`numbers` は `ADJUSTMENT_RANGES` と同じ順
/// （exposure, wb_temp, shadow_lift, highlight_recovery, brightness, contrast, saturation, sharpness）。
struct PresetDef {
    name: &'static str,
    label: &'static str,
    description: &'static str,
    numbers: [f64; 8],
    noise_reduction: &'static str,
}

/// 契約 §5 の表と 1 対 1 対応（`presets.py: PRESETS` と同じ順。数値は #81 で CI 向けに再調整し、Python とは違う）。
/// 数値はすべて f64 リテラルで書く（JSON で 15 と 15.0 を区別するため）。
const PRESETS: [PresetDef; 11] = [
    PresetDef {
        name: "neutral_recovery",
        label: "階調回復",
        description: "自然な明暗差を保つ標準仕上げ",
        numbers: [0.0, 0.0, 15.0, 0.0, 1.0, 1.03, 1.02, 1.04],
        noise_reduction: "light",
    },
    PresetDef {
        name: "landscape_clear",
        label: "風景クリア",
        description: "遠景の輪郭と色をくっきりさせる",
        // カメラ内JPEGの輪郭感に合わせた軽い輝度シャープ。色差は増幅しない。
        numbers: [0.0, 0.0, 10.0, 0.0, 1.0, 1.10, 1.08, 1.15],
        noise_reduction: "light",
    },
    PresetDef {
        name: "nightscape",
        label: "夜景ノイズ低減",
        description: "高ISO夜景のノイズを抑え暗部を持ち上げる",
        numbers: [0.0, -3.0, 15.0, 0.0, 1.0, 1.04, 1.0, 1.02],
        noise_reduction: "full",
    },
    PresetDef {
        name: "sunset_glow",
        label: "夕景・マジックアワー",
        description: "夕焼けの暖色を強め空の階調を守る",
        // 実効 (−0.7EV, lift 44) = C1' 標準 + 4（#81 run2: 中間調を CI 既定 ±0.03 に収める）。旧 −0.1EV は写像の ΔEV と二重になるので 0（設計書 §7.2）。
        numbers: [0.0, 12.0, 4.0, 60.0, 1.0, 1.05, 1.07, 1.03],
        noise_reduction: "light",
    },
    PresetDef {
        name: "indoor_warm",
        label: "室内・料理",
        description: "室内照明の暖かさを残し料理を明るく見せる",
        numbers: [0.1, 5.0, 18.0, 0.0, 1.0, 1.02, 1.03, 1.02],
        noise_reduction: "light",
    },
    PresetDef {
        name: "portrait_soft",
        label: "人物・柔らか",
        description: "肌を滑らかに見せる柔らかいコントラスト",
        numbers: [0.0, 3.0, 12.0, 0.0, 1.0, 0.98, 1.0, 0.96],
        noise_reduction: "light",
    },
    PresetDef {
        name: "monochrome_street",
        label: "モノクロ",
        description: "彩度を落としコントラストで見せる白黒仕上げ",
        numbers: [0.0, 0.0, 10.0, 0.0, 1.0, 1.08, 0.0, 1.04],
        noise_reduction: "light",
    },
    PresetDef {
        name: "highlight_rescue",
        label: "白飛び救済",
        description: "飛びかけた空や光源の階調を最大限引き戻す",
        // 実効 (−1.0EV, lift 55) = #92 で承認した C1' 強め。旧 −0.3 / 15 のままだと (−1.3, 70)（設計書 §11-4）。
        numbers: [0.0, 0.0, 0.0, 90.0, 1.0, 1.0, 1.0, 1.02],
        noise_reduction: "light",
    },
    PresetDef {
        name: "shadow_rescue",
        label: "黒つぶれ救済",
        description: "つぶれた暗部を大きく持ち上げ、増えるノイズを抑える",
        numbers: [0.0, 0.0, 45.0, 0.0, 1.0, 1.0, 1.0, 1.01],
        noise_reduction: "full",
    },
    PresetDef {
        name: "backlit_rescue",
        label: "逆光",
        description: "明るい背景の階調を残しつつ、影になった被写体を持ち上げる",
        // 実効 (−0.7EV, lift 70) = C1' 標準 + 被写体向けの lift 30。旧 50 / hr 80 のままだと lift 100 で頭打ち。
        numbers: [0.0, 0.0, 30.0, 60.0, 1.0, 1.0, 1.02, 1.02],
        noise_reduction: "light",
    },
    PresetDef {
        name: "vivid_travel",
        label: "ビビッド",
        description: "旅の記録向けに色を鮮やかに強調する",
        numbers: [0.0, 0.0, 10.0, 0.0, 1.0, 1.07, 1.10, 1.05],
        noise_reduction: "light",
    },
];

/// フロント `src/lib/types.ts` の `PresetInfo` と同じ形（契約 §1 の `presets[]`）。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PresetInfo {
    pub name: &'static str,
    pub label: &'static str,
    pub description: &'static str,
    /// 9 キー全量の Adjustments（数値は f64）。
    pub adjustments: Value,
}

impl PresetDef {
    /// 既定値の Adjustments（9 キー全量）。
    fn adjustments(&self) -> Map<String, Value> {
        let mut map = Map::new();
        for ((key, _, _), number) in ADJUSTMENT_RANGES.iter().zip(self.numbers) {
            map.insert((*key).to_string(), Value::from(number));
        }
        map.insert(
            NOISE_REDUCTION_KEY.to_string(),
            Value::from(self.noise_reduction),
        );
        map
    }
}

fn find_preset(name: &str) -> Option<&'static PresetDef> {
    PRESETS.iter().find(|preset| preset.name == name)
}

/// `GET /api/health` の `presets` と同じ `PresetInfo[]`（`presets.py: preset_infos()`）。
pub fn preset_infos() -> Vec<PresetInfo> {
    PRESETS
        .iter()
        .map(|preset| PresetInfo {
            name: preset.name,
            label: preset.label,
            description: preset.description,
            adjustments: Value::Object(preset.adjustments()),
        })
        .collect()
}

/// プリセット既定値に overrides をマージし、Adjustments 全キーを返す（契約 §9。
/// `presets.py: resolve_adjustments` と同じ規則）。
///
/// - 未知のプリセット名 → `UNKNOWN_PRESET`
/// - overrides がオブジェクトでない / 未知キー / 不正な型・非有限値 / 不正な noise_reduction
///   → `ADJUSTMENTS_INVALID`
/// - 数値の範囲外 → 契約 §2 の範囲へクランプ。数値は f64 に正規化
///
/// 現状 Tauri コマンドからは使っていない（フロントが全量を解決して渡し、
/// `develop_preview_coreimage` は `normalize_adjustments` で最後の検証をする）。本現像・候補生成を
/// Rust へ移すとき（#81 以降）に (preset, overrides) を受ける入口として使う。
#[cfg_attr(not(test), allow(dead_code))]
pub fn resolve_adjustments(
    preset_name: &str,
    overrides: Option<&Value>,
) -> Result<Value, CoreImageError> {
    let Some(preset) = find_preset(preset_name) else {
        return Err(CoreImageError::new(
            UNKNOWN_PRESET,
            // プリセット名は外部入力なので Debug 表記（引用符・エスケープ付き）で出す。
            format!("未対応のプリセットです: {preset_name:?}"),
        ));
    };
    let mut resolved = preset.adjustments();
    match overrides {
        None | Some(Value::Null) => {}
        Some(Value::Object(overrides)) => {
            for (key, value) in overrides {
                resolved.insert(key.clone(), value.clone());
            }
        }
        Some(_) => {
            return Err(CoreImageError::new(
                ADJUSTMENTS_INVALID,
                "adjustments はオブジェクトで指定してください",
            ));
        }
    }
    // 未知キーの拒否・型検査・クランプ・float 化は adjustments.rs の 1 か所で行う。
    normalize_adjustments(&Value::Object(resolved))
}

/// プリセット一覧。JS からは `invoke("get_presets")`。Python エンジンを経由せずに UI へ届ける経路。
#[tauri::command]
pub fn get_presets() -> Vec<PresetInfo> {
    preset_infos()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::adjustments::NOISE_REDUCTION_CHOICES;
    use serde_json::json;

    /// この表（CI 値）の写し。再生成は下の `write_presets_fixture`（#[ignore]）。
    const FIXTURE: &str = include_str!("../tests/fixtures/presets.json");
    const FIXTURE_PATH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/presets.json");

    /// フィクスチャの中身（プリセット表・値域・NR 選択肢）。
    fn fixture_payload() -> Value {
        let ranges: Map<String, Value> = ADJUSTMENT_RANGES
            .iter()
            .map(|(key, minimum, maximum)| (key.to_string(), json!([minimum, maximum])))
            .collect();
        json!({
            "presets": serde_json::to_value(preset_infos()).unwrap(),
            "adjustment_ranges": Value::Object(ranges),
            "noise_reduction_choices": json!(NOISE_REDUCTION_CHOICES),
        })
    }

    /// `tests/fixtures/presets.json` を書き出す（値を意図して変えたときだけ）。
    /// `UPDATE_PRESETS_FIXTURE=1 cargo test write_presets_fixture -- --ignored`
    #[test]
    #[ignore]
    fn write_presets_fixture() {
        if std::env::var("UPDATE_PRESETS_FIXTURE").as_deref() != Ok("1") {
            eprintln!("UPDATE_PRESETS_FIXTURE=1 のときだけ書き出す");
            return;
        }
        let text = serde_json::to_string_pretty(&fixture_payload()).unwrap() + "\n";
        std::fs::write(FIXTURE_PATH, text).expect("presets.json を書けること");
    }

    #[test]
    fn fixture_payload_round_trips() {
        assert_eq!(fixture_payload(), fixture());
    }

    #[test]
    fn highlight_recovery_only_on_rescue_presets() {
        // 案 B（設計書 §4、オーナー判断 2026-10-22）: hr > 0 は救済系 3 プリセットだけ。
        for info in preset_infos() {
            let hr = info.adjustments["highlight_recovery"].as_f64().unwrap();
            let rescue = matches!(
                info.name,
                "highlight_rescue" | "backlit_rescue" | "sunset_glow"
            );
            assert_eq!(hr > 0.0, rescue, "{} の hr = {hr}", info.name);
        }
    }

    fn fixture() -> Value {
        serde_json::from_str(FIXTURE).expect("presets.json が読めること")
    }

    fn code(result: Result<Value, CoreImageError>) -> String {
        result.unwrap_err().code
    }

    #[test]
    fn preset_infos_match_fixture_exactly() {
        // Value 同士の比較: 配列（プリセットの並び）は順序込み、数値は 15 と 15.0 を区別する。
        let rust = serde_json::to_value(preset_infos()).unwrap();
        assert_eq!(rust, fixture()["presets"]);
    }

    #[test]
    fn adjustment_ranges_match_fixture_exactly() {
        let ranges: Map<String, Value> = ADJUSTMENT_RANGES
            .iter()
            .map(|(key, minimum, maximum)| (key.to_string(), json!([minimum, maximum])))
            .collect();
        // オブジェクトの比較はキー順を問わない（値域は名前で引くので順序は意味を持たない）。
        assert_eq!(Value::Object(ranges), fixture()["adjustment_ranges"]);
        assert_eq!(
            json!(NOISE_REDUCTION_CHOICES),
            fixture()["noise_reduction_choices"]
        );
    }

    #[test]
    fn eleven_presets_with_unique_names_and_valid_values() {
        let infos = preset_infos();
        assert_eq!(infos.len(), 11);
        let mut names: Vec<&str> = infos.iter().map(|info| info.name).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), 11);
        assert!(find_preset(DEFAULT_PRESET).is_some());
        // 既定値は正規化しても変わらない（値域内・float 表記）。
        for info in &infos {
            assert_eq!(
                normalize_adjustments(&info.adjustments).unwrap(),
                info.adjustments,
                "{}",
                info.name
            );
        }
    }

    #[test]
    fn resolve_without_overrides_returns_preset_defaults() {
        for info in preset_infos() {
            assert_eq!(
                resolve_adjustments(info.name, None).unwrap(),
                info.adjustments
            );
            assert_eq!(
                resolve_adjustments(info.name, Some(&Value::Null)).unwrap(),
                info.adjustments
            );
            assert_eq!(
                resolve_adjustments(info.name, Some(&json!({}))).unwrap(),
                info.adjustments
            );
        }
    }

    #[test]
    fn resolve_merges_overrides_over_preset() {
        let resolved = resolve_adjustments(
            "nightscape",
            Some(&json!({"exposure": 0.5, "noise_reduction": "off"})),
        )
        .unwrap();
        assert_eq!(resolved["exposure"], json!(0.5));
        assert_eq!(resolved["noise_reduction"], json!("off"));
        // 上書きしていないキーはプリセット値のまま。
        assert_eq!(resolved["wb_temp"], json!(-3.0));
        assert_eq!(resolved["shadow_lift"], json!(15.0));
    }

    #[test]
    fn resolve_rejects_unknown_preset() {
        assert_eq!(code(resolve_adjustments("nope", None)), UNKNOWN_PRESET);
        assert_eq!(
            code(resolve_adjustments("Neutral_Recovery", None)),
            UNKNOWN_PRESET,
            "大文字小文字は区別する"
        );
        assert_eq!(code(resolve_adjustments("", None)), UNKNOWN_PRESET);
    }

    #[test]
    fn resolve_rejects_unknown_keys() {
        // Python: test_raw_engine の未知キー拒否と同じ。
        assert_eq!(
            code(resolve_adjustments(
                "neutral_recovery",
                Some(&json!({"exp_shift": 1.0}))
            )),
            ADJUSTMENTS_INVALID
        );
        assert_eq!(
            code(resolve_adjustments(
                "neutral_recovery",
                Some(&json!({"exposure": 0.0, "unknown": 1}))
            )),
            ADJUSTMENTS_INVALID
        );
    }

    #[test]
    fn resolve_rejects_non_object_overrides() {
        for bad in [json!([]), json!("x"), json!(1), json!(true)] {
            assert_eq!(
                code(resolve_adjustments("neutral_recovery", Some(&bad))),
                ADJUSTMENTS_INVALID,
                "{bad}"
            );
        }
    }

    #[test]
    fn resolve_clamps_both_ends() {
        // Python と同じ結果: exposure 5 → 2.0、-5 → -2.0、sharpness 0.1 → 0.8、wb_temp 999 → 100.0。
        let cases = [
            ("exposure", json!(5), json!(2.0)),
            ("exposure", json!(-5), json!(-2.0)),
            ("sharpness", json!(0.1), json!(0.8)),
            ("sharpness", json!(3.0), json!(1.6)),
            ("wb_temp", json!(999), json!(100.0)),
            ("saturation", json!(-1.0), json!(0.0)),
            ("brightness", json!(0.0), json!(0.7)),
            ("highlight_recovery", json!(150), json!(100.0)),
        ];
        for (key, input, expected) in cases {
            let resolved =
                resolve_adjustments("neutral_recovery", Some(&json!({ key: input }))).unwrap();
            assert_eq!(resolved[key], expected, "{key} = {input}");
        }
    }

    #[test]
    fn resolve_normalizes_integers_to_floats() {
        // Python: `resolve_adjustments(..., {"shadow_lift": 20})["shadow_lift"]` は 20.0（float）。
        let resolved =
            resolve_adjustments("neutral_recovery", Some(&json!({"shadow_lift": 20}))).unwrap();
        assert!(resolved["shadow_lift"].is_f64());
        assert_eq!(resolved["shadow_lift"].to_string(), "20.0");
        let as_float =
            resolve_adjustments("neutral_recovery", Some(&json!({"shadow_lift": 20.0}))).unwrap();
        assert_eq!(resolved.to_string(), as_float.to_string());
    }

    #[test]
    fn resolve_rejects_bad_types() {
        for bad in [json!(true), json!("1.0"), json!([1.0]), json!({"v": 1})] {
            assert_eq!(
                code(resolve_adjustments(
                    "neutral_recovery",
                    Some(&json!({ "contrast": bad }))
                )),
                ADJUSTMENTS_INVALID,
                "{bad}"
            );
        }
    }

    #[test]
    fn resolve_noise_reduction_enum() {
        for choice in NOISE_REDUCTION_CHOICES {
            let resolved = resolve_adjustments(
                "neutral_recovery",
                Some(&json!({ "noise_reduction": choice })),
            )
            .unwrap();
            assert_eq!(resolved["noise_reduction"], json!(choice));
        }
        for bad in [json!("strong"), json!("Light"), json!(""), json!(1)] {
            assert_eq!(
                code(resolve_adjustments(
                    "neutral_recovery",
                    Some(&json!({ "noise_reduction": bad }))
                )),
                ADJUSTMENTS_INVALID,
                "{bad}"
            );
        }
    }

    #[test]
    fn preset_info_serializes_in_frontend_shape() {
        let value = serde_json::to_value(&preset_infos()[0]).unwrap();
        let keys: Vec<&str> = value
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        let mut sorted = keys.clone();
        sorted.sort_unstable();
        assert_eq!(sorted, ["adjustments", "description", "label", "name"]);
        assert_eq!(value["adjustments"].as_object().unwrap().len(), 9);
    }
}
