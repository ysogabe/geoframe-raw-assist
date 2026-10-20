import CoreImage
import Foundation
import XCTest

@testable import CoreImageDevelop

/// 後段チェーン（Metal カーネル）の単体テスト（設計書 §3.7「PostChainTests」）。ARW 不要。
///
/// 合成画像を RGBAf の CIImage にし、`PostChain.applyKernels` だけを通して読み戻す。入力・出力とも
/// colorSpace nil（色管理なし）なので、比べているのはカーネルの式と作業フォーマット（RGBAh = half float）の
/// 精度だけで、8bit 量子化やトーンカーブの往復は入らない。参照は local_raw/engine.py の numpy 式を
/// Swift で書き直したもの（`ReferenceChain`）。
final class PostChainTests: XCTestCase {
    /// develop() と同じ作業空間・作業フォーマット。
    static let context = CIContext(options: [
        .workingColorSpace: CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!,
        .workingFormat: CIFormat.RGBAh,
        .cacheIntermediates: false,
    ])

    // MARK: 画像の入出力

    struct RGBImage {
        let width: Int
        let height: Int
        var pixels: [SIMD3<Double>]

        subscript(x: Int, y: Int) -> SIMD3<Double> {
            get { pixels[y * width + x] }
            set { pixels[y * width + x] = newValue }
        }
    }

    static func makeCIImage(_ image: RGBImage) -> CIImage {
        var floats = [Float](repeating: 1, count: image.width * image.height * 4)
        for (i, p) in image.pixels.enumerated() {
            floats[i * 4] = Float(p.x)
            floats[i * 4 + 1] = Float(p.y)
            floats[i * 4 + 2] = Float(p.z)
        }
        let data = floats.withUnsafeBufferPointer { Data(buffer: $0) }
        return CIImage(
            bitmapData: data, bytesPerRow: image.width * 16,
            size: CGSize(width: image.width, height: image.height), format: .RGBAf, colorSpace: nil
        )
    }

    static func render(_ ciImage: CIImage, width: Int, height: Int) -> RGBImage {
        var floats = [Float](repeating: 0, count: width * height * 4)
        floats.withUnsafeMutableBytes { buffer in
            context.render(
                ciImage, toBitmap: buffer.baseAddress!, rowBytes: width * 16,
                bounds: CGRect(x: 0, y: 0, width: width, height: height), format: .RGBAf, colorSpace: nil
            )
        }
        var pixels = [SIMD3<Double>]()
        pixels.reserveCapacity(width * height)
        for i in 0..<(width * height) {
            pixels.append(SIMD3(Double(floats[i * 4]), Double(floats[i * 4 + 1]), Double(floats[i * 4 + 2])))
        }
        return RGBImage(width: width, height: height, pixels: pixels)
    }

    /// 256 段の水平グラデーション（灰・色付き）+ 乱数の色パッチ + 範囲外の値 + 1 本の階段エッジ。
    static func syntheticImage() -> RGBImage {
        let width = 256
        let height = 12
        var image = RGBImage(width: width, height: height, pixels: Array(repeating: .zero, count: width * height))
        var seed: UInt64 = 0x9E37_79B9_7F4A_7C15
        func random() -> Double {
            seed = seed &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
            return Double(seed >> 11) / Double(1 << 53)
        }
        for x in 0..<width {
            let g = Double(x) / 255.0
            image[x, 0] = SIMD3(g, g, g)
            image[x, 1] = SIMD3(g, g, g)
            image[x, 2] = SIMD3(g, 0.3, 0.1)
            image[x, 3] = SIMD3(0.9, g, 1.0 - g)
            image[x, 4] = SIMD3(random(), random(), random())
            image[x, 5] = SIMD3(random(), random(), random())
            // CIRAWFilter の extended 出力を模した範囲外の値（tone カーネル冒頭のクリップを確かめる）。
            image[x, 6] = SIMD3(-0.1 + 1.3 * g, 1.2 - 1.3 * g, 0.5)
            for y in 7..<height {
                let step = x < 128 ? 0.2 : 0.8
                image[x, y] = SIMD3(step, step * 0.9, step * 0.7)
            }
        }
        return image
    }

    // MARK: 参照実装（local_raw/engine.py の numpy 式）

    enum ReferenceChain {
        static func luma(_ p: SIMD3<Double>) -> Double {
            p.x * 0.2126 + p.y * 0.7152 + p.z * 0.0722
        }

        /// `_apply_shadow_lift_array` → `_apply_tone_array`（brightness → contrast → saturation）。クリップ前。
        static func tone(_ p: SIMD3<Double>, _ params: PostChainParameters) -> SIMD3<Double> {
            var x = p.clamped(lowerBound: .zero, upperBound: .one)
            if params.lift > 0 {
                // SHADOW_LIFT_GAIN / 255 = 2.0
                x = (x + params.lift * 2.0 * x * (.one - x) * (.one - x)).clamped(lowerBound: .zero, upperBound: .one)
            }
            x *= params.brightness
            x = (x - SIMD3(repeating: 0.5)) * params.contrast + SIMD3(repeating: 0.5)
            let y = luma(x)
            if params.saturation == 0 {
                return SIMD3(repeating: y)
            }
            return SIMD3(repeating: y) + (x - SIMD3(repeating: y)) * params.saturation
        }

        /// cv2.GaussianBlur（float 画像）と同じ離散核: `ksize = round(8σ + 1) | 1`、`exp(−i²/2σ²)` を正規化。
        /// 端は複製（CI の clampedToExtent と同じ。cv2 既定の BORDER_REFLECT_101 とは端の 3 画素だけ違う）。
        static func gaussianBlur(_ values: [Double], width: Int, height: Int, sigma: Double) -> [Double] {
            let ksize = Int((sigma * 8 + 1).rounded()) | 1
            let radius = (ksize - 1) / 2
            var kernel = (-radius...radius).map { exp(-Double($0 * $0) / (2 * sigma * sigma)) }
            let sum = kernel.reduce(0, +)
            kernel = kernel.map { $0 / sum }
            func pass(_ src: [Double], horizontal: Bool) -> [Double] {
                var out = [Double](repeating: 0, count: src.count)
                for y in 0..<height {
                    for x in 0..<width {
                        var acc = 0.0
                        for (k, w) in kernel.enumerated() {
                            let o = k - radius
                            let sx = horizontal ? min(width - 1, max(0, x + o)) : x
                            let sy = horizontal ? y : min(height - 1, max(0, y + o))
                            acc += w * src[sy * width + sx]
                        }
                        out[y * width + x] = acc
                    }
                }
                return out
            }
            return pass(pass(values, horizontal: true), horizontal: false)
        }

        /// tone → 輝度 USM（コアリング）→ [0,1] クリップ。
        static func apply(_ image: RGBImage, _ params: PostChainParameters) -> RGBImage {
            var toned = image
            toned.pixels = image.pixels.map { tone($0, params) }
            if params.usmGain > 0 {
                let y = toned.pixels.map(luma)
                let blurred = gaussianBlur(y, width: image.width, height: image.height, sigma: params.usmSigma)
                for i in toned.pixels.indices {
                    var d = y[i] - blurred[i]
                    d = (d < 0 ? -1.0 : (d > 0 ? 1.0 : 0.0)) * max(abs(d) - params.usmCoring, 0)
                    toned.pixels[i] += SIMD3(repeating: d * params.usmGain)
                }
            }
            toned.pixels = toned.pixels.map { $0.clamped(lowerBound: .zero, upperBound: .one) }
            return toned
        }
    }

    static func maxError(_ a: RGBImage, _ b: RGBImage, rows: Range<Int>? = nil, columns: Range<Int>? = nil) -> Double {
        var worst = 0.0
        for y in rows ?? 0..<a.height {
            for x in columns ?? 0..<a.width {
                let diff = a[x, y] - b[x, y]
                worst = max(worst, abs(diff.x), abs(diff.y), abs(diff.z))
            }
        }
        return worst
    }

    // MARK: テスト

    func testEmbeddedMetalLibraryProvidesBothKernels() throws {
        XCTAssertNotNil(KernelLibrary.data)
        XCTAssertFalse(KernelLibrary.sourceSHA256.isEmpty)
        let kernels = try PostChain.kernels.get()
        XCTAssertNotNil(kernels.tone)
        XCTAssertNotNil(kernels.usm)
    }

    func testEmbeddedLibraryMatchesKernelSource() throws {
        // 生成物が .metal と食い違っていないか（.metal を変えて scripts/build-coreimage-kernels.sh を忘れた場合に落ちる）。
        let source = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Kernels/GeoFrameKernels.metal")
        guard let data = try? Data(contentsOf: source) else {
            throw XCTSkip("kernel source not found at \(source.path)")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/shasum")
        process.arguments = ["-a", "256", source.path]
        let pipe = Pipe()
        process.standardOutput = pipe
        try process.run()
        process.waitUntilExit()
        let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        XCTAssertFalse(data.isEmpty)
        XCTAssertEqual(output.split(separator: " ").first.map(String.init), KernelLibrary.sourceSHA256,
                       "Generated/KernelLibrary.swift is stale: run scripts/build-coreimage-kernels.sh")
    }

    func testToneKernelMatchesNumpyFormula() throws {
        let input = Self.syntheticImage()
        let cases: [PostChainParameters] = [
            PostChainParameters(lift: 0, brightness: 1, contrast: 1, saturation: 1, usmSigma: 0.64, usmGain: 0, usmCoring: 0),
            PostChainParameters(lift: 0.15, brightness: 1.0, contrast: 1.03, saturation: 1.02, usmSigma: 0.64, usmGain: 0, usmCoring: 0),
            PostChainParameters(lift: 0.45, brightness: 1.4, contrast: 1.4, saturation: 1.5, usmSigma: 0.64, usmGain: 0, usmCoring: 0),
            PostChainParameters(lift: 1.0, brightness: 0.7, contrast: 0.8, saturation: 0, usmSigma: 0.64, usmGain: 0, usmCoring: 0),
            PostChainParameters(lift: 0.3, brightness: 1.2, contrast: 1.2, saturation: 0.5, usmSigma: 0.64, usmGain: 0, usmCoring: 0),
        ]
        for params in cases {
            let output = try PostChain.applyKernels(Self.makeCIImage(input), params)
            XCTAssertEqual(output.extent, CGRect(x: 0, y: 0, width: input.width, height: input.height))
            let got = Self.render(output, width: input.width, height: input.height)
            let want = ReferenceChain.apply(input, params)
            let error = Self.maxError(got, want)
            // 許容 1.5/255（half float。設計書 §3.7 の【推測】値）。
            XCTAssertLessThanOrEqual(error * 255, 1.5, "\(params) max error \(error * 255)/255")
        }
    }

    func testSaturationZeroIsMonochrome() throws {
        let input = Self.syntheticImage()
        let params = PostChainParameters(lift: 0, brightness: 1, contrast: 1, saturation: 0, usmSigma: 0.64, usmGain: 0, usmCoring: 0)
        let got = Self.render(try PostChain.applyKernels(Self.makeCIImage(input), params), width: input.width, height: input.height)
        for p in got.pixels {
            XCTAssertEqual(p.x, p.y, accuracy: 1e-3)
            XCTAssertEqual(p.y, p.z, accuracy: 1e-3)
        }
    }

    func testUSMMatchesNumpyFormula() throws {
        let input = Self.syntheticImage()
        let cases: [PostChainParameters] = [
            // 実用域（preview 2400px・ISO 100・sharpness 1.6 相当）。
            PostChainParameters(lift: 0.15, brightness: 1.0, contrast: 1.03, saturation: 1.02, usmSigma: 0.64, usmGain: 0.486, usmCoring: 0),
            // 高 ISO（コアリング 1/255）・原寸 σ。
            PostChainParameters(lift: 0, brightness: 1, contrast: 1, saturation: 1, usmSigma: 0.8, usmGain: 0.63, usmCoring: 1.0 / 255.0),
            // 大きい σ・強い gain（ぼかし幅の違いが出やすい）。
            PostChainParameters(lift: 0, brightness: 1.1, contrast: 1.2, saturation: 1, usmSigma: 2.0, usmGain: 1.2, usmCoring: 0.5 / 255.0),
        ]
        for params in cases {
            let got = Self.render(try PostChain.applyKernels(Self.makeCIImage(input), params), width: input.width, height: input.height)
            let want = ReferenceChain.apply(input, params)
            // 行方向の境界（合成画像の行の継ぎ目）を含むので全画素で比べる。
            let error = Self.maxError(got, want)
            // 暫定（ハーネスで較正）: ガウス核の離散化の違い（CI と参照）を含めて 3/255 以内。
            XCTAssertLessThanOrEqual(error * 255, 3.0, "\(params) max error \(error * 255)/255")
        }
    }

    /// 1 行の 10–90% 立ち上がり幅（線形補間）。
    static func riseWidth(_ row: [Double]) -> Double {
        let lo = row.first!
        let hi = row.last!
        func crossing(_ level: Double) -> Double {
            let target = lo + (hi - lo) * level
            for i in 1..<row.count where (row[i - 1] - target) * (row[i] - target) <= 0 && row[i] != row[i - 1] {
                return Double(i - 1) + (target - row[i - 1]) / (row[i] - row[i - 1])
            }
            return .nan
        }
        return crossing(0.9) - crossing(0.1)
    }

    /// USM のぼかし幅が σ どおりであること（設計書 §3.7・§6 リスク 6）。
    /// 階段エッジのぼかしの 10–90% 幅を測り、
    /// - 全 σ: rawpy（cv2.GaussianBlur と同じ離散核 = `ReferenceChain.gaussianBlur`）の同じ測り方の幅と ±10% 以内
    /// - σ ≥ 2（CIGaussianBlur 経路。`inputRadius` = σ の確認）: 連続ガウスの 2.563σ と ±10% 以内、
    ///   画素中心でサンプルした解析解（erf）とも ±10% 以内
    func testGaussianBlurWidthMatchesSigma() throws {
        let width = 128
        let height = 8
        var image = RGBImage(width: width, height: height, pixels: Array(repeating: .zero, count: width * height))
        for y in 0..<height {
            for x in 64..<width {
                image[x, y] = SIMD3(repeating: 1)
            }
        }
        let step = (0..<width).map { $0 < 64 ? 0.0 : 1.0 }
        for sigma in [0.6, 0.64, 0.8, 1.0, 2.0, 3.0] {
            let blurred = PostChain.blurredLuminance(Self.makeCIImage(image), sigma: sigma)
            let got = Self.render(blurred, width: width, height: height)
            let row = (0..<width).map { got[$0, height / 2].x }
            let measured = Self.riseWidth(row)
            let reference = Self.riseWidth(ReferenceChain.gaussianBlur(step, width: width, height: 1, sigma: sigma))
            XCTAssertEqual(measured, reference, accuracy: reference * 0.1, "sigma \(sigma): width \(measured) vs cv2-like \(reference)")
            if sigma >= 2 {
                // エッジは x = 63.5（画素 63 と 64 の境界）。画素中心 x の値 = Φ((x − 63.5)/σ)。
                let analytic = (0..<width).map { 0.5 * (1 + erf((Double($0) - 63.5) / (sigma * 2.0.squareRoot()))) }
                let expected = Self.riseWidth(analytic)
                XCTAssertEqual(measured, expected, accuracy: expected * 0.1, "sigma \(sigma): width \(measured) vs erf \(expected)")
                XCTAssertEqual(measured, 2.563 * sigma, accuracy: 2.563 * sigma * 0.1, "sigma \(sigma)")
            }
            // clampedToExtent で端が暗くならない（左端は 0、右端は 1 のまま）。
            XCTAssertEqual(row.first!, 0, accuracy: 1e-3)
            XCTAssertEqual(row.last!, 1, accuracy: 1e-3)
        }
    }

    func testSmallSigmaUsesDiscreteKernelLikeCV2() {
        // cv2.getGaussianKernel(7, 0.6 / 0.8) と同じ核（float 画像の ksize 規則で半径 3）。
        let k06 = PostChain.gaussianKernel(sigma: 0.6)
        XCTAssertEqual(k06.count, 7)
        XCTAssertEqual(k06.reduce(0, +), 1, accuracy: 1e-12)
        XCTAssertEqual(k06[3], 1 / (1 + 2 * exp(-1 / 0.72) + 2 * exp(-4 / 0.72) + 2 * exp(-9 / 0.72)), accuracy: 1e-12)
        XCTAssertEqual(PostChain.gaussianKernel(sigma: 0.8).count, 7)
        XCTAssertEqual(PostChain.gaussianKernel(sigma: 1.0).count, 9)
        XCTAssertGreaterThan(PostChain.gaussianKernel(sigma: 2.0).count, 9)
    }

    func testBlurredLuminanceMatchesDiscreteGaussian() throws {
        let input = Self.syntheticImage()
        let y = input.pixels.map(ReferenceChain.luma)
        for sigma in [0.6, 0.64, 0.8] {
            let got = Self.render(PostChain.blurredLuminance(Self.makeCIImage(input), sigma: sigma), width: input.width, height: input.height)
            let want = ReferenceChain.gaussianBlur(y, width: input.width, height: input.height, sigma: sigma)
            var worst = 0.0
            for i in want.indices {
                worst = max(worst, abs(got.pixels[i].x - want[i]))
            }
            XCTAssertLessThanOrEqual(worst * 255, 0.5, "sigma \(sigma) max error \(worst * 255)/255")
        }
    }

    func testFullChainIdentityKeepsInRangeLinearValues() throws {
        // apply()（linear → sRGB → カーネル → linear）は恒等パラメータでほぼ素通し（half float の往復誤差のみ）。
        let input = Self.syntheticImage()
        let params = PostChainParameters(lift: 0, brightness: 1, contrast: 1, saturation: 1, usmSigma: 0.64, usmGain: 0, usmCoring: 0)
        let got = Self.render(try PostChain.apply(Self.makeCIImage(input), params), width: input.width, height: input.height)
        var clipped = input
        clipped.pixels = input.pixels.map { $0.clamped(lowerBound: .zero, upperBound: .one) }
        XCTAssertLessThanOrEqual(Self.maxError(got, clipped) * 255, 1.0)
    }
}
