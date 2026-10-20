import CoreImage
import Foundation

// 後段チェーン（Issue #73、設計書 docs/design/coreimage-preview-adjustments.md §3.2 手順 8）。
// rawpy の apply_post_decode_chain（shadow_lift → resize → brightness → contrast → saturation → 輝度 USM → クリップ）
// を CIRAWFilter.outputImage の後ろに組む。resize は CIRAWFilter.scaleFactor（デコーダ内縮小）が先に済ませている。
// カーネルは Kernels/GeoFrameKernels.metal（埋め込みは Generated/KernelLibrary.swift）。

/// 後段に実際に使う値（応答 `adjustments_applied.post` の元）。
public struct PostChainParameters: Equatable {
    /// shadow_lift / 100（0..1）。
    public var lift: Double
    public var brightness: Double
    public var contrast: Double
    public var saturation: Double
    /// USM の σ（px）。`CIGaussianBlur.inputRadius` へそのまま渡す（単体テストで σ 相当であることを確認）。
    public var usmSigma: Double
    /// 0 なら USM 段を作らない。
    public var usmGain: Double
    /// コアリング閾値（0..1 スケール）。
    public var usmCoring: Double

    public init(lift: Double, brightness: Double, contrast: Double, saturation: Double, usmSigma: Double, usmGain: Double, usmCoring: Double) {
        self.lift = lift
        self.brightness = brightness
        self.contrast = contrast
        self.saturation = saturation
        self.usmSigma = usmSigma
        self.usmGain = usmGain
        self.usmCoring = usmCoring
    }
}

public enum PostChain {
    static let toneFunction = "geoframe_tone"
    static let usmFunction = "geoframe_usm"

    /// 埋め込み metallib から読んだカーネル（プロセス内で 1 回だけ読む）。
    static let kernels: Result<(tone: CIColorKernel, usm: CIColorKernel), DevelopError> = {
        guard let data = KernelLibrary.data else {
            return .failure(DevelopError(.internalError, "embedded Metal kernel library is not valid base64"))
        }
        do {
            let tone = try CIColorKernel(functionName: toneFunction, fromMetalLibraryData: data)
            let usm = try CIColorKernel(functionName: usmFunction, fromMetalLibraryData: data)
            return .success((tone, usm))
        } catch {
            return .failure(DevelopError(.internalError, "failed to load Metal kernels: \(error)"))
        }
    }()

    /// 作業空間（extended linear sRGB）の画像に後段チェーンを掛ける。`develop()` が使う。
    /// clamp(0..1) → sRGB ガンマへ → カーネル（`applyKernels`）→ リニアへ戻す。
    public static func apply(_ linear: CIImage, _ params: PostChainParameters) throws -> CIImage {
        let extent = linear.extent
        // rawpy の _apply_shadow_lift_array 冒頭の [0,1] クリップに相当（extended 範囲の値を落とす）。
        let clamped = linear.applyingFilter("CIColorClamp", parameters: [
            "inputMinComponents": CIVector(x: 0, y: 0, z: 0, w: 0),
            "inputMaxComponents": CIVector(x: 1, y: 1, z: 1, w: 1),
        ])
        let gamma = clamped.applyingFilter("CILinearToSRGBToneCurve")
        let processed = try applyKernels(gamma, params)
        return processed.applyingFilter("CISRGBToneCurveToLinear").cropped(to: extent)
    }

    /// sRGB ガンマ空間の 0..1 の画像にカーネルを掛ける（色変換を含まない。単体テストはここを直接検証する）。
    /// tone（lift → clip → brightness → contrast → saturation）→ [USM gain > 0 なら] 輝度 USM + [0,1] クリップ、
    /// gain 0 なら [0,1] クリップのみ。
    public static func applyKernels(_ gamma: CIImage, _ params: PostChainParameters) throws -> CIImage {
        let (toneKernel, usmKernel) = try kernels.get()
        let extent = gamma.extent
        guard let toned = toneKernel.apply(extent: extent, arguments: [
            gamma, Float(params.lift), Float(params.brightness), Float(params.contrast), Float(params.saturation),
        ]) else {
            throw DevelopError(.renderFailed, "geoframe_tone kernel produced no image")
        }

        guard params.usmGain > 0 else {
            return toned.applyingFilter("CIColorClamp", parameters: [
                "inputMinComponents": CIVector(x: 0, y: 0, z: 0, w: 0),
                "inputMaxComponents": CIVector(x: 1, y: 1, z: 1, w: 1),
            ]).cropped(to: extent)
        }

        let blurredLuma = blurredLuminance(toned, sigma: params.usmSigma)
        guard let sharpened = usmKernel.apply(extent: extent, arguments: [
            toned, blurredLuma, Float(params.usmGain), Float(params.usmCoring),
        ]) else {
            throw DevelopError(.renderFailed, "geoframe_usm kernel produced no image")
        }
        return sharpened.cropped(to: extent)
    }

    /// Rec.709 輝度 Y を RGB に複製した画像をガウスぼかしする。端は clampedToExtent で延長してから
    /// ぼかして extent に切り戻す（透明な縁を混ぜて端が暗くなるのを防ぐ）。
    ///
    /// σ が小さい（核の半径 ≤ 4）ときは cv2.GaussianBlur と同じ離散核（`gaussianKernel`）を
    /// CIConvolution9Horizontal / Vertical で掛ける。CIGaussianBlur は inputRadius ≈ σ で階段エッジの幅は
    /// 合う（PostChainTests）が、σ 0.6〜0.8 では高周波の応答が離散ガウスと違い、乱数パッチで
    /// 最大 10/255・平均 1.9/255 ずれた（σ 0.64）。rawpy の USM の σ は 0.6〜0.8 なのでこちらを使う。
    /// 半径 5 以上（σ ≳ 1.06）は 9 タップに収まらないので CIGaussianBlur に任せる。
    static func blurredLuminance(_ image: CIImage, sigma: Double) -> CIImage {
        let extent = image.extent
        let y = CIVector(x: 0.2126, y: 0.7152, z: 0.0722, w: 0)
        let luma = image.applyingFilter("CIColorMatrix", parameters: [
            "inputRVector": y, "inputGVector": y, "inputBVector": y,
            "inputAVector": CIVector(x: 0, y: 0, z: 0, w: 1),
            "inputBiasVector": CIVector(x: 0, y: 0, z: 0, w: 0),
        ]).clampedToExtent()

        let kernel = gaussianKernel(sigma: sigma)
        let blurred: CIImage
        if kernel.count <= 9 {
            // 9 タップの中央に寄せ、余りは 0 で埋める。
            let pad = (9 - kernel.count) / 2
            var weights = [CGFloat](repeating: 0, count: 9)
            for (i, w) in kernel.enumerated() {
                weights[pad + i] = CGFloat(w)
            }
            let vector = CIVector(values: weights, count: 9)
            blurred = luma
                .applyingFilter("CIConvolution9Horizontal", parameters: [kCIInputWeightsKey: vector, kCIInputBiasKey: 0])
                .applyingFilter("CIConvolution9Vertical", parameters: [kCIInputWeightsKey: vector, kCIInputBiasKey: 0])
        } else {
            blurred = luma.applyingFilter("CIGaussianBlur", parameters: [kCIInputRadiusKey: sigma])
        }
        return blurred.cropped(to: extent)
    }

    /// cv2.getGaussianKernel と同じ 1 次元の離散ガウス核（float 画像の核サイズ規則
    /// `ksize = round(σ·4·2 + 1) | 1`、`exp(−i²/2σ²)` を正規化）。
    static func gaussianKernel(sigma: Double) -> [Double] {
        let ksize = Int((sigma * 4 * 2 + 1).rounded()) | 1
        let radius = (ksize - 1) / 2
        let raw = (-radius...radius).map { exp(-Double($0 * $0) / (2 * sigma * sigma)) }
        let sum = raw.reduce(0, +)
        return raw.map { $0 / sum }
    }
}
