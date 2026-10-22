import CoreImage
import Foundation
import XCTest

@testable import CoreImageDevelop

/// Adjustments → CIRAWFilter / 後段の写像（設計書 §3.3・§3.4・§3.7「AdjustmentMappingTests」）。ARW 不要。
final class AdjustmentMappingTests: XCTestCase {
    /// rawpy の `local_raw.engine._capture_sharpen_params(long_edge, sharpness, iso)` の実出力。
    /// 生成: `PYTHONPATH=. .venv/bin/python` で長辺 × ISO（nil = 不明）× sharpness を総当たりして repr を写した。
    /// (long_edge, sharpness, iso, sigma, gain, threshold)
    static let rawpyTable: [(Int, Double, Double?, Double, Double, Double)] = [
        (1600, 0.8, 100, 0.6, 0.35000000000000003, 0.0),
        (1600, 1.0, 100, 0.6, 0.5, 0.0),
        (1600, 1.6, 100, 0.6, 0.9500000000000001, 0.0),
        (1600, 0.8, 800, 0.6, 0.35000000000000003, 0.00130718954248366),
        (1600, 1.0, 800, 0.6, 0.5, 0.00130718954248366),
        (1600, 1.6, 800, 0.6, 0.9500000000000001, 0.00130718954248366),
        (1600, 0.8, 2400, 0.6, 0.2625, 0.00392156862745098),
        (1600, 1.0, 2400, 0.6, 0.375, 0.00392156862745098),
        (1600, 1.6, 2400, 0.6, 0.7125, 0.00392156862745098),
        (1600, 0.8, 3200, 0.6, 0.17500000000000002, 0.00392156862745098),
        (1600, 1.0, 3200, 0.6, 0.25, 0.00392156862745098),
        (1600, 1.6, 3200, 0.6, 0.47500000000000003, 0.00392156862745098),
        (1600, 0.8, 6400, 0.6, 0.17500000000000002, 0.00392156862745098),
        (1600, 1.0, 6400, 0.6, 0.25, 0.00392156862745098),
        (1600, 1.6, 6400, 0.6, 0.47500000000000003, 0.00392156862745098),
        (1600, 0.8, nil, 0.6, 0.35000000000000003, 0.00196078431372549),
        (1600, 1.0, nil, 0.6, 0.5, 0.00196078431372549),
        (1600, 1.6, nil, 0.6, 0.9500000000000001, 0.00196078431372549),
        (2400, 0.8, 100, 0.64, 0.37800000000000006, 0.0),
        (2400, 1.0, 100, 0.64, 0.54, 0.0),
        (2400, 1.6, 100, 0.64, 1.0260000000000002, 0.0),
        (2400, 0.8, 800, 0.64, 0.37800000000000006, 0.00130718954248366),
        (2400, 1.0, 800, 0.64, 0.54, 0.00130718954248366),
        (2400, 1.6, 800, 0.64, 1.0260000000000002, 0.00130718954248366),
        (2400, 0.8, 2400, 0.64, 0.28350000000000003, 0.00392156862745098),
        (2400, 1.0, 2400, 0.64, 0.405, 0.00392156862745098),
        (2400, 1.6, 2400, 0.64, 0.7695000000000002, 0.00392156862745098),
        (2400, 0.8, 3200, 0.64, 0.18900000000000003, 0.00392156862745098),
        (2400, 1.0, 3200, 0.64, 0.27, 0.00392156862745098),
        (2400, 1.6, 3200, 0.64, 0.5130000000000001, 0.00392156862745098),
        (2400, 0.8, 6400, 0.64, 0.18900000000000003, 0.00392156862745098),
        (2400, 1.0, 6400, 0.64, 0.27, 0.00392156862745098),
        (2400, 1.6, 6400, 0.64, 0.5130000000000001, 0.00392156862745098),
        (2400, 0.8, nil, 0.64, 0.37800000000000006, 0.00196078431372549),
        (2400, 1.0, nil, 0.64, 0.54, 0.00196078431372549),
        (2400, 1.6, nil, 0.64, 1.0260000000000002, 0.00196078431372549),
        (6000, 0.8, 100, 0.8, 0.49, 0.0),
        (6000, 1.0, 100, 0.8, 0.7, 0.0),
        (6000, 1.6, 100, 0.8, 1.2, 0.0),
        (6000, 0.8, 800, 0.8, 0.49, 0.00130718954248366),
        (6000, 1.0, 800, 0.8, 0.7, 0.00130718954248366),
        (6000, 1.6, 800, 0.8, 1.2, 0.00130718954248366),
        (6000, 0.8, 2400, 0.8, 0.3675, 0.00392156862745098),
        (6000, 1.0, 2400, 0.8, 0.5249999999999999, 0.00392156862745098),
        (6000, 1.6, 2400, 0.8, 0.8999999999999999, 0.00392156862745098),
        (6000, 0.8, 3200, 0.8, 0.245, 0.00392156862745098),
        (6000, 1.0, 3200, 0.8, 0.35, 0.00392156862745098),
        (6000, 1.6, 3200, 0.8, 0.6, 0.00392156862745098),
        (6000, 0.8, 6400, 0.8, 0.245, 0.00392156862745098),
        (6000, 1.0, 6400, 0.8, 0.35, 0.00392156862745098),
        (6000, 1.6, 6400, 0.8, 0.6, 0.00392156862745098),
        (6000, 0.8, nil, 0.8, 0.49, 0.00196078431372549),
        (6000, 1.0, nil, 0.8, 0.7, 0.00196078431372549),
        (6000, 1.6, nil, 0.8, 1.2, 0.00196078431372549),
    ]

    func testCaptureSharpenParamsMatchesRawpy() {
        for (edge, sharpness, iso, sigma, gain, threshold) in Self.rawpyTable {
            let got = rawpyCaptureSharpenParams(longEdge: edge, sharpness: sharpness, iso: iso)
            let label = "edge \(edge) s \(sharpness) iso \(iso.map { "\($0)" } ?? "nil")"
            XCTAssertEqual(got.sigma, sigma, accuracy: 1e-12, label)
            XCTAssertEqual(got.gain, gain, accuracy: 1e-12, label)
            XCTAssertEqual(got.threshold, threshold, accuracy: 1e-12, label)
        }
    }

    func testRawpyConstantsAreCopiedVerbatim() {
        XCTAssertEqual(RawpySharpen.smallEdge, 2000)
        XCTAssertEqual(RawpySharpen.largeEdge, 4000)
        XCTAssertEqual(RawpySharpen.sigmaSmall, 0.6)
        XCTAssertEqual(RawpySharpen.sigmaLarge, 0.8)
        XCTAssertEqual(RawpySharpen.gainSmall, 0.5)
        XCTAssertEqual(RawpySharpen.gainLarge, 0.7)
        XCTAssertEqual(RawpySharpen.sliderSlope, 1.5)
        XCTAssertEqual(RawpySharpen.gainMax, 1.2)
        XCTAssertEqual(RawpySharpen.isoCoringStart, 400)
        XCTAssertEqual(RawpySharpen.isoCoringFull, 1600)
        XCTAssertEqual(RawpySharpen.isoGainFullAtten, 3200)
        XCTAssertEqual(RawpySharpen.isoGainFloor, 0.5)
        XCTAssertEqual(RawpySharpen.coringMax, 1.0 / 255.0)
        XCTAssertEqual(RawpySharpen.coringUnknownISO, 0.5 / 255.0)
    }

    func testCoreImageUSMGainIsIncrementOnly() {
        let base = captureSharpenBase(longEdge: 2400, iso: 100)
        XCTAssertEqual(base.sigma, 0.64, accuracy: 1e-12)
        XCTAssertEqual(base.baseGain, 0.54, accuracy: 1e-12)
        // s ≤ 1 は USM 無し（デコーダ内シャープ側で弱める）。
        XCTAssertEqual(usmGainForCoreImage(sharpness: 0.8, base: base), 0)
        XCTAssertEqual(usmGainForCoreImage(sharpness: 1.0, base: base), 0)
        // s > 1 は増分だけ: baseGain × (s − 1) × 1.5。
        XCTAssertEqual(usmGainForCoreImage(sharpness: 1.6, base: base), 0.54 * 0.6 * 1.5, accuracy: 1e-12)
        // ISO 6400 は gain を 0.5 倍に減衰、コアリング 1/255。
        let high = captureSharpenBase(longEdge: 2400, iso: 6400)
        XCTAssertEqual(high.isoMultiplier, 0.5)
        XCTAssertEqual(high.coring, 1.0 / 255.0)
        XCTAssertEqual(usmGainForCoreImage(sharpness: 1.6, base: high), 0.54 * 0.6 * 1.5 * 0.5, accuracy: 1e-12)
        // 原寸側でも GAIN_MAX（1.2）を超えない。
        let large = captureSharpenBase(longEdge: 6000, iso: nil)
        XCTAssertLessThanOrEqual(usmGainForCoreImage(sharpness: 1.6, base: large), 1.2)
        XCTAssertEqual(large.coring, 0.5 / 255.0)
    }

    func testDecoderSharpnessIsScaledOnlyAtOrBelowOne() {
        XCTAssertEqual(decoderSharpnessAmount(sharpness: 0.8, defaultAmount: 1.0)!, 0.8, accuracy: 1e-12)
        XCTAssertEqual(decoderSharpnessAmount(sharpness: 0.8, defaultAmount: 0.81)!, 0.648, accuracy: 1e-12)
        XCTAssertNil(decoderSharpnessAmount(sharpness: 1.0, defaultAmount: 1.0))
        XCTAssertNil(decoderSharpnessAmount(sharpness: 1.6, defaultAmount: 1.0))
    }

    func testWhiteBalanceShiftIsMonotonicAndBounded() {
        let k0 = 5000.0
        let zero = WhiteBalanceMapping.shift(k0: k0, wbTemp: 0)
        XCTAssertEqual(zero.kelvin, k0, accuracy: 1e-9)
        XCTAssertEqual(zero.miredShift, 0, accuracy: 1e-9)
        // v > 0（暖）→ mired 減 → K 増。単調。
        var previous = 0.0
        for v in stride(from: -100.0, through: 100.0, by: 10.0) {
            let k = WhiteBalanceMapping.shift(k0: k0, wbTemp: v).kelvin
            XCTAssertGreaterThan(k, previous, "v \(v)")
            previous = k
        }
        let warm = WhiteBalanceMapping.shift(k0: k0, wbTemp: 60)
        XCTAssertEqual(warm.miredShift, -60 * WhiteBalanceMapping.miredPerUnit, accuracy: 1e-9)
        XCTAssertEqual(warm.kelvin, 1e6 / (200 - 51), accuracy: 1e-6)
        // 端: K は 2000...50000 に収まる（mired が 0 以下で発散しない）。
        let hot = WhiteBalanceMapping.shift(k0: 40000, wbTemp: 100)
        XCTAssertEqual(hot.kelvin, 50000, accuracy: 1e-6)
        XCTAssertEqual(hot.miredShift, 20 - 25, accuracy: 1e-9)
        let cold = WhiteBalanceMapping.shift(k0: 2100, wbTemp: -100)
        XCTAssertEqual(cold.kelvin, 2000, accuracy: 1e-6)
        XCTAssertTrue(WhiteBalanceMapping.kelvinRange.contains(cold.kelvin))
    }

    func testEncodingSurvivesNonFiniteReadbacks() {
        // 読み戻しが NaN でも応答（guards を含む）が INTERNAL_ERROR にならず書ける
        let defaults = AdjustmentsApplied.Defaults(
            sharpness_amount: 1, luminance_noise_reduction_amount: 0, color_noise_reduction_amount: 0,
            neutral_temperature: .nan, neutral_tint: 0
        )
        let line = encodeJSONLine(defaults)
        XCTAssertTrue(line.contains("\"neutral_temperature\":\"nan\""), line)
        XCTAssertFalse(line.contains("INTERNAL_ERROR"))
    }

    func testK0UsabilityGuardsBrokenMetadata() {
        XCTAssertTrue(WhiteBalanceMapping.isUsableK0(5000))
        XCTAssertTrue(WhiteBalanceMapping.isUsableK0(2000))
        XCTAssertTrue(WhiteBalanceMapping.isUsableK0(50000))
        let bad: [Double] = [.nan, .infinity, -.infinity, 0, -5000, 1999.9, 50000.1, 1e9]
        for value in bad {
            XCTAssertFalse(WhiteBalanceMapping.isUsableK0(value), "\(value)")
        }
        XCTAssertEqual(WhiteBalanceMapping.invalidK0Guard, "wb_k0_out_of_range")
    }

    func testUsableExtentRejectsInfiniteAndEmpty() {
        XCTAssertTrue(isUsableExtent(CGRect(x: 0, y: 0, width: 2400, height: 1600)))
        XCTAssertTrue(isUsableExtent(CGRect(x: 0.5, y: -0.25, width: 10.2, height: 3.9)))
        XCTAssertFalse(isUsableExtent(CGRect.infinite))
        XCTAssertFalse(isUsableExtent(CGRect.null))
        XCTAssertFalse(isUsableExtent(CGRect(x: 0, y: 0, width: 0, height: 100)))
        XCTAssertFalse(isUsableExtent(CGRect(x: 0, y: 0, width: 100, height: 0)))
        XCTAssertFalse(isUsableExtent(CGRect(x: 0, y: 0, width: CGFloat.infinity, height: 100)))
        XCTAssertFalse(isUsableExtent(CGRect(x: CGFloat.nan, y: 0, width: 100, height: 100)))
    }

    func testNoiseReductionTable() {
        // off は (0, 0)、light は既定のまま、full は max(既定, min(0.5, max(既定 + 0.3, 0.3))) / 色 0.3 以上（T5 で確定）。
        XCTAssertEqual(NoiseReductionMapping.fullLuminanceCap, 0.5)
        let cases: [(Double, Double, NoiseReduction, Double?, Double?)] = [
            (0.01, 0, .off, 0, 0),
            (0.241, 0, .off, 0, 0),
            (0.241, 0, .light, nil, nil),
            (0.01, 0, .full, 0.31, 0.3),
            (0.0, 0, .full, 0.3, 0.3),
            (0.15, 0, .full, 0.45, 0.3),
            (0.241, 0, .full, 0.5, 0.3),
            (0.357, 0, .full, 0.5, 0.3),
            (0.5, 0.5, .full, 0.5, 0.5),
            (0.9, 0.5, .full, 0.9, 0.5),
        ]
        for (lum, color, mode, wantLum, wantColor) in cases {
            let got = NoiseReductionMapping.amounts(mode, defaultLuminance: lum, defaultColor: color)
            let label = "\(mode) lum \(lum) color \(color)"
            if let wantLum { XCTAssertEqual(got.luminance!, wantLum, accuracy: 1e-12, label) } else { XCTAssertNil(got.luminance, label) }
            if let wantColor { XCTAssertEqual(got.color!, wantColor, accuracy: 1e-12, label) } else { XCTAssertNil(got.color, label) }
        }
    }

    func testFullNoiseReductionNeverWeakerThanLightAtHighISO() {
        // 既定（= light）が上限 0.5 を超える高 ISO では、full は上限で切らず既定のまま（light 以上）にする。
        for lum in [0.5, 0.501, 0.6, 0.75, 1.0] {
            let got = NoiseReductionMapping.amounts(.full, defaultLuminance: lum, defaultColor: 0)
            XCTAssertEqual(got.luminance!, lum, accuracy: 1e-12, "lum \(lum)")
            XCTAssertGreaterThanOrEqual(got.luminance!, lum, "full は light（既定）より弱くしない: lum \(lum)")
        }
        // 既定が上限未満なら従来どおり上限 0.5 で頭打ち。
        let below = NoiseReductionMapping.amounts(.full, defaultLuminance: 0.49, defaultColor: 0)
        XCTAssertEqual(below.luminance!, 0.5, accuracy: 1e-12)
    }

    func testWhiteDotGuardCondition() {
        XCTAssertTrue(WhiteDotGuard.needsGuard(luminanceNoiseReduction: 0, sharpness: 0, scaleFactor: 0.4))
        XCTAssertFalse(WhiteDotGuard.needsGuard(luminanceNoiseReduction: 0, sharpness: 0, scaleFactor: 1))
        XCTAssertFalse(WhiteDotGuard.needsGuard(luminanceNoiseReduction: 0.24, sharpness: 0, scaleFactor: 0.4))
        XCTAssertFalse(WhiteDotGuard.needsGuard(luminanceNoiseReduction: 0, sharpness: 0.1, scaleFactor: 0.4))
        XCTAssertEqual(WhiteDotGuard.sharpnessFloor, 0.1)
        XCTAssertEqual(WhiteDotGuard.name, "white_dot_sharpness_floor")
        // 通常のスライダー範囲（sharpness ≥ 0.8 × 既定 0.81）では床上げに当たらない。
        XCTAssertGreaterThan(decoderSharpnessAmount(sharpness: 0.8, defaultAmount: 0.81)!, Double(WhiteDotGuard.sharpnessFloor))
    }

    func testAdjustmentsAppliedEncodingKeepsNullISO() {
        let usm = AdjustmentsApplied.USM(sigma: 0.64, gain: 0, coring: 0.5 / 255, long_edge: 2400, iso: nil)
        let json = encodeJSONLine(usm)
        XCTAssertTrue(json.contains(#""iso":null"#), json)
        XCTAssertTrue(json.contains(#""long_edge":2400"#), json)
    }

    func testCapabilitiesAdvertiseAdjustments() throws {
        XCTAssertEqual(cliVersion, "0.6.0")
        let json = encodeJSONLine(Capabilities(probe: nil))
        // 0.6.0（#81）で highlight_recovery が効くようになり ignored は空配列（フィールドは残す）。
        XCTAssertTrue(json.contains(#""adjustments":{"ignored":[],"post_kernel":"metal","supported":true}"#), json)
    }

    // MARK: highlight_recovery の写像（#81、設計書 coreimage-preset-retune.md §2・§3）

    func testHighlightRecoveryNodesAreExact() {
        // 節点では表の値をそのまま返す（補間の丸めを通さない。§2.2 の等価性の前提）。
        let zero = HighlightRecoveryMapping.delta(0)
        XCTAssertEqual(zero.exposure, 0)
        XCTAssertEqual(zero.shadowLift, 0)
        let c1 = HighlightRecoveryMapping.delta(60)
        XCTAssertEqual(c1.exposure, -0.7)
        XCTAssertEqual(c1.shadowLift, 40)
        let strong = HighlightRecoveryMapping.delta(90)
        XCTAssertEqual(strong.exposure, -1.0)
        XCTAssertEqual(strong.shadowLift, 55)
        let top = HighlightRecoveryMapping.delta(100)
        XCTAssertEqual(top.exposure, -1.1)
        XCTAssertEqual(top.shadowLift, 60)
    }

    func testHighlightRecoveryInterpolatesLinearlyBetweenNodes() {
        let cases: [(Double, Double, Double)] = [
            (30, -0.35, 20), (50, -0.7 * 50 / 60, 40 * 50 / 60.0), (70, -0.8, 45), (80, -0.9, 50), (95, -1.05, 57.5),
        ]
        for (hr, ev, lift) in cases {
            let d = HighlightRecoveryMapping.delta(hr)
            XCTAssertEqual(d.exposure, ev, accuracy: 1e-12, "hr \(hr)")
            XCTAssertEqual(d.shadowLift, lift, accuracy: 1e-12, "hr \(hr)")
        }
    }

    func testHighlightRecoveryIsMonotonic() {
        var previous = HighlightRecoveryMapping.delta(0)
        for step in 1...200 {
            let d = HighlightRecoveryMapping.delta(Double(step) * 0.5)
            XCTAssertLessThanOrEqual(d.exposure, previous.exposure, "hr \(Double(step) * 0.5)")
            XCTAssertGreaterThanOrEqual(d.shadowLift, previous.shadowLift, "hr \(Double(step) * 0.5)")
            previous = d
        }
    }

    func testHighlightRecoveryZeroIsIdentity() {
        let adjustments = Adjustments(exposure: 0.3, shadowLift: 15, highlightRecovery: 0)
        let resolved = HighlightRecoveryMapping.resolve(adjustments)
        XCTAssertEqual(resolved.adjustments, adjustments)
        XCTAssertEqual(resolved.effect.exposureDelta, 0)
        XCTAssertEqual(resolved.effect.shadowLiftDelta, 0)
        XCTAssertFalse(resolved.effect.shadowLiftClamped)
    }

    func testHighlightRecoveryAddsToPresetValues() {
        // 加算合成（§3）。hr は記録用に残る。
        let resolved = HighlightRecoveryMapping.resolve(Adjustments(exposure: -0.3, shadowLift: 15, highlightRecovery: 90))
        XCTAssertEqual(resolved.adjustments.exposure, -1.3, accuracy: 1e-12)
        XCTAssertEqual(resolved.adjustments.shadowLift, 70, accuracy: 1e-12)
        XCTAssertEqual(resolved.adjustments.highlightRecovery, 90)
        XCTAssertEqual(resolved.effect.effectiveExposure, -1.3, accuracy: 1e-12)
        XCTAssertEqual(resolved.effect.effectiveShadowLift, 70, accuracy: 1e-12)
    }

    func testHighlightRecoveryClampsLiftAt100() {
        let resolved = HighlightRecoveryMapping.resolve(Adjustments(shadowLift: 50, highlightRecovery: 90))
        XCTAssertEqual(resolved.effect.effectiveShadowLift, 100)
        XCTAssertEqual(resolved.adjustments.shadowLift, 100)
        XCTAssertTrue(resolved.effect.shadowLiftClamped)
        let edge = HighlightRecoveryMapping.resolve(Adjustments(shadowLift: 40, highlightRecovery: 100))
        XCTAssertEqual(edge.effect.effectiveShadowLift, 100)
        XCTAssertFalse(edge.effect.shadowLiftClamped)
    }

    func testHighlightRecoveryEquivalentToApprovedC1Settings() {
        // §2.2: {0, 0, hr 60} ≡ {−0.7, 40, hr 0}、{0, 0, hr 90} ≡ {−1.0, 55, hr 0}（実効値が厳密に一致）。
        for (hr, ev, lift) in [(60.0, -0.7, 40.0), (90.0, -1.0, 55.0)] {
            let viaHR = HighlightRecoveryMapping.resolve(Adjustments(highlightRecovery: hr)).adjustments
            let direct = HighlightRecoveryMapping.resolve(Adjustments(exposure: ev, shadowLift: lift)).adjustments
            XCTAssertEqual(viaHR.exposure, direct.exposure, "hr \(hr)")
            XCTAssertEqual(viaHR.shadowLift, direct.shadowLift, "hr \(hr)")
            XCTAssertEqual(Float(viaHR.exposure), Float(direct.exposure))
            XCTAssertEqual(viaHR.shadowLift / 100.0, direct.shadowLift / 100.0)
        }
    }

    func testAdjustmentsAppliedEncodesHighlightRecovery() {
        let effect = HighlightRecoveryMapping.effect(Adjustments(shadowLift: 15, highlightRecovery: 60))
        let json = encodeJSONLine(AdjustmentsApplied.HighlightRecovery(effect))
        XCTAssertEqual(
            json,
            #"{"effective_exposure":-0.7,"effective_shadow_lift":55,"exposure_delta":-0.7,"shadow_lift_clamped":false,"shadow_lift_delta":40,"value":60}"#
        )
    }

    func testSuccessResponseOmitsAdjustmentsAppliedByDefault() throws {
        let applied = Applied(
            profile: "apple_default", decoder_version: "9", scale_factor: 0.4, exposure: 0, baseline_exposure: 0.4,
            boost_amount: 1, boost_shadow_amount: 0.9, shadow_bias: 3, neutral_temperature: 5000, neutral_tint: 0,
            sharpness_amount: 1, contrast_amount: 0, detail_amount: 0, luminance_noise_reduction_amount: 0.24,
            color_noise_reduction_amount: 0, moire_reduction_amount: 0, local_tone_map_amount: 0,
            extended_dynamic_range_amount: 0, gamut_mapping_enabled: true, draft_mode_enabled: false,
            lens_correction_enabled: true, lens_correction_supported: true, local_tone_map_supported: false,
            orientation: 1, working_color_space: "w", color_space: "c", bit_depth: 8, orientation_handling: "h"
        )
        let source = SourceInfo(camera_model: nil, lens_model: nil, iso: nil, native_width: 6000, native_height: 4000, orientation: 1)
        let response = SuccessResponse(
            cli_version: cliVersion, macos_version: "27.0.1", decoder_version: "9", input: "/a.ARW", output: "/b.jpg",
            mode: "preview", format: "jpeg", width: 2400, height: 1600,
            elapsed_ms: ElapsedMS(decode: 1, render_encode: 1, total: 2), applied: applied, source: source
        )
        let json = encodeJSONLine(response)
        XCTAssertFalse(json.contains("adjustments_applied"), json)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
        XCTAssertEqual(Set(object.keys), [
            "ok", "engine", "cli_version", "macos_version", "decoder_version", "input", "output", "mode", "format",
            "width", "height", "elapsed_ms", "applied", "source",
        ])
    }
}
