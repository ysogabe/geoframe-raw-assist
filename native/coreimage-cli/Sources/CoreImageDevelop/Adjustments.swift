import Foundation

// Adjustments（アプリのスライダー 9 キー）→ CIRAWFilter / 後段チェーンへの写像（Issue #73）。
// 設計書 docs/design/coreimage-preview-adjustments.md §3.1・§3.3・§3.4。
// rawpy 側の式・定数は local_raw/engine.py（_capture_sharpen_params ほか）から移植している。
// ここは純粋関数だけを置く（CIRAWFilter に触るのは Develop.swift）。

/// `adjustments.noise_reduction`。
public enum NoiseReduction: String, Equatable {
    case off
    case light
    case full
}

/// 検証済みの Adjustments（9 キー全量）。`RequestParser.parseAdjustments` だけが作る。
public struct Adjustments: Equatable {
    public var exposure: Double
    public var wbTemp: Double
    public var shadowLift: Double
    /// 受理するが効かせない（no-op。`adjustments_applied.ignored` に載る。代替は #81）。
    public var highlightRecovery: Double
    public var brightness: Double
    public var contrast: Double
    public var saturation: Double
    public var sharpness: Double
    public var noiseReduction: NoiseReduction

    public init(
        exposure: Double = 0, wbTemp: Double = 0, shadowLift: Double = 0, highlightRecovery: Double = 0,
        brightness: Double = 1, contrast: Double = 1, saturation: Double = 1, sharpness: Double = 1,
        noiseReduction: NoiseReduction = .light
    ) {
        self.exposure = exposure
        self.wbTemp = wbTemp
        self.shadowLift = shadowLift
        self.highlightRecovery = highlightRecovery
        self.brightness = brightness
        self.contrast = contrast
        self.saturation = saturation
        self.sharpness = sharpness
        self.noiseReduction = noiseReduction
    }

    /// 受理するが効かせないキー（応答 `adjustments_applied.ignored`、`--capabilities`）。
    public static let ignoredKeys = ["highlight_recovery"]

    /// 数値 8 キーの値域（契約 docs/API_CONTRACT.md §2 = local_raw/presets.py: ADJUSTMENT_RANGES）。
    /// CLI はクランプしない。範囲外は INVALID_REQUEST（クランプは Rust / Python の責務）。
    static let numericKeys: [(key: String, range: ClosedRange<Double>, set: (inout Adjustments, Double) -> Void)] = [
        ("exposure", -2.0...2.0, { $0.exposure = $1 }),
        ("wb_temp", -100.0...100.0, { $0.wbTemp = $1 }),
        ("shadow_lift", 0.0...100.0, { $0.shadowLift = $1 }),
        ("highlight_recovery", 0.0...100.0, { $0.highlightRecovery = $1 }),
        ("brightness", 0.7...1.4, { $0.brightness = $1 }),
        ("contrast", 0.8...1.4, { $0.contrast = $1 }),
        ("saturation", 0.0...1.5, { $0.saturation = $1 }),
        ("sharpness", 0.8...1.6, { $0.sharpness = $1 }),
    ]

    static var allKeys: Set<String> { Set(numericKeys.map(\.key) + ["noise_reduction"]) }
}

// MARK: - 撮影時シャープ（rawpy の _capture_sharpen_params の移植）

/// local_raw/engine.py の撮影時シャープ定数（値はそのまま移植。変えるときは engine.py と揃えること）。
public enum RawpySharpen {
    public static let smallEdge = 2000.0
    public static let largeEdge = 4000.0
    public static let sigmaSmall = 0.6
    public static let sigmaLarge = 0.8
    public static let gainSmall = 0.5
    public static let gainLarge = 0.7
    public static let sliderSlope = 1.5
    public static let gainMax = 1.2
    public static let isoCoringStart = 400.0
    public static let isoCoringFull = 1600.0
    public static let isoGainFullAtten = 3200.0
    public static let isoGainFloor = 0.5
    public static let coringMax = 1.0 / 255.0
    public static let coringUnknownISO = 0.5 / 255.0
}

/// 出力長辺と ISO で決まる USM の基準値（sharpness に依存しない部分）。
public struct CaptureSharpenBase: Equatable {
    public let sigma: Double
    /// sharpness 1.0 のときの gain（ISO 減衰前）。
    public let baseGain: Double
    /// コアリング閾値（0..1 スケール）。
    public let coring: Double
    /// ISO による gain 減衰の倍率（1.0 = 減衰なし）。
    public let isoMultiplier: Double
}

func clamp01(_ value: Double) -> Double {
    min(1.0, max(0.0, value))
}

/// `_capture_sharpen_params` のうち sharpness に依存しない部分。
public func captureSharpenBase(longEdge: Int, iso: Double?) -> CaptureSharpenBase {
    typealias K = RawpySharpen
    let t = clamp01((Double(longEdge) - K.smallEdge) / (K.largeEdge - K.smallEdge))
    let sigma = K.sigmaSmall + (K.sigmaLarge - K.sigmaSmall) * t
    let baseGain = K.gainSmall + (K.gainLarge - K.gainSmall) * t
    let coring: Double
    let isoMultiplier: Double
    if let iso {
        coring = K.coringMax * clamp01((iso - K.isoCoringStart) / (K.isoCoringFull - K.isoCoringStart))
        let attenuation = clamp01((iso - K.isoCoringFull) / (K.isoGainFullAtten - K.isoCoringFull))
        isoMultiplier = 1.0 - (1.0 - K.isoGainFloor) * attenuation
    } else {
        coring = K.coringUnknownISO
        isoMultiplier = 1.0
    }
    return CaptureSharpenBase(sigma: sigma, baseGain: baseGain, coring: coring, isoMultiplier: isoMultiplier)
}

/// rawpy の `_capture_sharpen_params(long_edge, sharpness, iso)` と同じ (sigma, gain, threshold)。
/// CI 経路では使わない（rawpy は 1.0 でも基準 gain を掛けるが、CI は apple_default のデコーダ内シャープが
/// 1.0 の基準なので増分だけを足す。`usmGainForCoreImage`）。移植が正しいことを単体テストで確かめるための関数。
public func rawpyCaptureSharpenParams(longEdge: Int, sharpness: Double, iso: Double?) -> (sigma: Double, gain: Double, threshold: Double) {
    let base = captureSharpenBase(longEdge: longEdge, iso: iso)
    var gain = base.baseGain * (1.0 + (sharpness - 1.0) * RawpySharpen.sliderSlope)
    gain = min(RawpySharpen.gainMax, max(0.0, gain))
    return (base.sigma, gain * base.isoMultiplier, base.coring)
}

/// CI 経路の後段 USM の gain（設計書 §3.3）。s ≤ 1 は 0（デコーダ内シャープを弱める側で表現する）。
/// s > 1 は増分だけ: `min(GAIN_MAX, baseGain × (s − 1) × SLOPE) × isoMultiplier`。
public func usmGainForCoreImage(sharpness: Double, base: CaptureSharpenBase) -> Double {
    guard sharpness > 1.0 else { return 0.0 }
    let gain = min(RawpySharpen.gainMax, base.baseGain * (sharpness - 1.0) * RawpySharpen.sliderSlope)
    return max(0.0, gain) * base.isoMultiplier
}

/// sharpness ≤ 1 のときデコーダへ書く `sharpnessAmount`（= 既定 × s）。s > 1 と s == 1 は nil（既定のまま）。
public func decoderSharpnessAmount(sharpness: Double, defaultAmount: Double) -> Double? {
    guard sharpness < 1.0 else { return nil }
    return min(1.0, max(0.0, defaultAmount * sharpness))
}

// MARK: - wb_temp（カメラ WB の K を mired でずらす）

public enum WhiteBalanceMapping {
    /// wb_temp 1 単位あたりの mired シフト量。**暫定（ハーネスで較正）**: 設計書 §3.3 の推測値。
    /// rawpy は R×(1+0.003v)・B×(1−0.003v) で、それと同じ大きさになる係数を T5 で決める。
    public static let miredPerUnit = 0.85
    /// CIRAWFilter.neutralTemperature に書く K の範囲（リクエスト settings.neutral_temperature と同じ）。
    public static let kelvinRange = RequestParser.temperatureRange

    /// `K0`（撮影時 WB の色温度）と `wb_temp` から書き込む K を求める。
    /// `mired = 1e6/K0 − v × MIRED_PER_UNIT`、K は kelvinRange にクランプ。v > 0（暖）で K が増える。
    /// 返す `miredShift` はクランプ後の実効値（1e6/K − 1e6/K0）。
    public static func shift(k0: Double, wbTemp: Double) -> (kelvin: Double, miredShift: Double) {
        let mired0 = 1e6 / k0
        // mired の範囲 = 1e6/50000 (20) ... 1e6/2000 (500)。0 以下で発散しないよう先に mired でクランプする。
        let lo = 1e6 / kelvinRange.upperBound
        let hi = 1e6 / kelvinRange.lowerBound
        let mired = min(hi, max(lo, mired0 - wbTemp * miredPerUnit))
        let kelvin = min(kelvinRange.upperBound, max(kelvinRange.lowerBound, 1e6 / mired))
        return (kelvin, 1e6 / kelvin - mired0)
    }
}

// MARK: - noise_reduction（apple_default の既定値を基準にした相対写像）

/// `noise_reduction` → (luminanceNoiseReductionAmount, colorNoiseReductionAmount)。nil = 既定のまま。
///
/// - off: (0, 0)
/// - light: 既定のまま（apple_default は低 ISO でほぼ 0、高 ISO で効く。rawpy の light と同じ形）
/// - full: `lum = min(1, max(既定 + FULL_LUM_BOOST, FULL_LUM_FLOOR))`、`color = max(既定, FULL_COLOR_FLOOR)`
///
/// ISO 帯別の目安（macOS 27.0.1 / decoder 9 の既定から。README「noise_reduction の表」）:
/// ISO < 800 既定 ≈0.01/0 → full 0.31/0.30、ISO 3200 0.241/0 → full 0.541/0.30、ISO 6400 0.357/0 → full 0.657/0.30。
public enum NoiseReductionMapping {
    /// **暫定（ハーネスで較正）**: full で既定の輝度 NR に足す量。
    public static let fullLuminanceBoost = 0.3
    /// **暫定（ハーネスで較正）**: full の輝度 NR の下限。
    public static let fullLuminanceFloor = 0.3
    /// **暫定（ハーネスで較正）**: full の色 NR の下限。
    public static let fullColorFloor = 0.3

    public static func amounts(
        _ mode: NoiseReduction, defaultLuminance: Double, defaultColor: Double
    ) -> (luminance: Double?, color: Double?) {
        switch mode {
        case .off:
            return (0.0, 0.0)
        case .light:
            return (nil, nil)
        case .full:
            let luminance = min(1.0, max(defaultLuminance + fullLuminanceBoost, fullLuminanceFloor))
            let color = min(1.0, max(defaultColor, fullColorFloor))
            return (luminance, color)
        }
    }
}

// MARK: - 白点ガード（設計書 §1.4・§3.4）

/// decoder 9 は「輝度 NR 0 ∧ sharpness 0 ∧ scaleFactor < 1」のときだけ白点ノイズを出す（比較レポート §12.3）。
/// `adjustments` 指定時に限り、この組み合わせを作らないよう sharpnessAmount を床上げする。
public enum WhiteDotGuard {
    public static let name = "white_dot_sharpness_floor"
    /// 床上げ後の sharpnessAmount。sharpness 0.1 で白点が消えることを確認済み（比較レポート §12.3）。
    /// 最小限の値として設計書 §3.4 が指定したもの。**暫定（ハーネスで較正）**: impulse_vs_full で回帰確認する。
    public static let sharpnessFloor: Float = 0.1

    public static func needsGuard(luminanceNoiseReduction: Float, sharpness: Float, scaleFactor: Float) -> Bool {
        luminanceNoiseReduction == 0 && sharpness == 0 && scaleFactor < 1
    }
}
