import Foundation
import ImageIO
import XCTest

@testable import CoreImageDevelop

/// 実 ARW がある環境でだけ走るスモークテスト（無ければ skip）。入力は読むだけ。
final class SmokeTests: XCTestCase {
    static let landscape = "/Volumes/Backup/100MSDCF/DSC04430.ARW"
    static let portrait = "/Volumes/Backup/100MSDCF/DSC04553.ARW"
    /// 個別上書き（#79）のスモーク用。外付けボリュームが無い環境でも走るよう、星空サンプル
    /// （`scripts/eval_cases/candidates_baseline.json` の `stars` ルート。拡張子は小文字 .arw）も候補にする。
    /// 環境変数 `GEOFRAME_CI_SMOKE_ARW` で任意の ARW を指すこともできる。
    static let highISOCandidates = [
        ProcessInfo.processInfo.environment["GEOFRAME_CI_SMOKE_ARW"],
        "/Volumes/Backup/100MSDCF/DSC04430.ARW",
        NSString(string: "~/works/60-misc/40-raw-assistant/work/raw/DSC07415.arw").expandingTildeInPath,
    ].compactMap { $0 }

    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("ci-smoke-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private func requireFile(_ path: String) throws {
        guard FileManager.default.isReadableFile(atPath: path) else {
            throw XCTSkip("sample RAW not available: \(path)")
        }
    }

    private func run(_ json: String) throws -> SuccessResponse {
        try develop(try RequestParser.parse(Data(json.utf8)))
    }

    private func firstReadable(_ candidates: [String]) throws -> String {
        guard let path = candidates.first(where: { FileManager.default.isReadableFile(atPath: $0) }) else {
            throw XCTSkip("sample RAW not available: \(candidates.joined(separator: ", "))")
        }
        return path
    }

    func testLandscapePreviewNeutralJPEG() throws {
        try requireFile(Self.landscape)
        let output = directory.appendingPathComponent("land.jpg")
        let response = try run(#"{"input":"\#(Self.landscape)","output":"\#(output.path)","mode":"preview","max_edge":600,"decoder_version":"8"}"#)
        XCTAssertEqual(response.width, 600)
        XCTAssertEqual(response.height, 400)
        XCTAssertEqual(response.decoder_version, "8")
        XCTAssertEqual(response.applied.sharpness_amount, 0)
        XCTAssertEqual(response.applied.luminance_noise_reduction_amount, 0)
        XCTAssertEqual(response.applied.color_noise_reduction_amount, 0)
        XCTAssertEqual(response.applied.contrast_amount, 0)
        XCTAssertFalse(response.applied.lens_correction_enabled)
        XCTAssertFalse(response.applied.draft_mode_enabled)
        XCTAssertEqual(response.applied.boost_amount, 1)
        XCTAssertEqual(response.source.camera_model, "ILCE-6400")
        XCTAssertEqual(response.source.native_width, 6000)

        let source = try XCTUnwrap(CGImageSourceCreateWithURL(output as CFURL, nil))
        let props = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any])
        XCTAssertEqual(props[kCGImagePropertyProfileName as String] as? String, "sRGB IEC61966-2.1")
        XCTAssertNil(props[kCGImagePropertyGPSDictionary as String])
    }

    func testAppleDefaultKeepsFilterDefaultsButHonoursLensFlag() throws {
        try requireFile(Self.landscape)
        let output = directory.appendingPathComponent("default.tif")
        let response = try run(#"{"input":"\#(Self.landscape)","output":"\#(output.path)","mode":"preview","max_edge":300,"format":"tiff16","settings":{"profile":"apple_default","lens_correction":true}}"#)
        XCTAssertEqual(response.applied.sharpness_amount, 1)
        XCTAssertTrue(response.applied.lens_correction_enabled)
        XCTAssertEqual(response.applied.color_space, "Display P3")
        XCTAssertEqual(response.applied.bit_depth, 16)
    }

    func testAppleDefaultOverridesAreAppliedAndReadBack() throws {
        let input = try firstReadable(Self.highISOCandidates)
        let output = directory.appendingPathComponent("override.jpg")
        let response = try run("""
        {"input":"\(input)","output":"\(output.path)","mode":"preview","max_edge":300,"decoder_version":"9",
         "settings":{"profile":"apple_default","lens_correction":true,"luminance_noise_reduction":0,"color_noise_reduction":0,
                     "boost_shadow_amount":1.2}}
        """)
        XCTAssertEqual(response.applied.profile, "apple_default")
        XCTAssertEqual(response.applied.luminance_noise_reduction_amount, 0)
        XCTAssertEqual(response.applied.color_noise_reduction_amount, 0)
        XCTAssertEqual(response.applied.boost_shadow_amount, 1.2, accuracy: 1e-6)
        // 上書きしていない値は apple_default の既定のまま（sharpness は ISO で変わるので 0 でないことだけ見る）。
        XCTAssertGreaterThan(response.applied.sharpness_amount, 0)
        XCTAssertEqual(response.applied.boost_amount, 1)
        XCTAssertTrue(response.applied.lens_correction_enabled)
    }

    func testTIFF16SRGBOutputHasSRGBProfile() throws {
        let input = try firstReadable(Self.highISOCandidates)
        let output = directory.appendingPathComponent("srgb.tif")
        let response = try run("""
        {"input":"\(input)","output":"\(output.path)","mode":"preview","max_edge":300,"format":"tiff16","output_color_space":"srgb",
         "decoder_version":"9","settings":{"profile":"apple_default","lens_correction":true,"luminance_noise_reduction":0}}
        """)
        XCTAssertEqual(response.applied.color_space, "sRGB IEC61966-2.1")
        XCTAssertEqual(response.applied.bit_depth, 16)
        XCTAssertEqual(response.applied.luminance_noise_reduction_amount, 0)
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(output as CFURL, nil))
        let props = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any])
        XCTAssertEqual(props[kCGImagePropertyProfileName as String] as? String, "sRGB IEC61966-2.1")
        XCTAssertEqual(props[kCGImagePropertyDepth as String] as? Int, 16)
        XCTAssertNil(props[kCGImagePropertyGPSDictionary as String])
    }

    func testPortraitIsWrittenUpright() throws {
        try requireFile(Self.portrait)
        let output = directory.appendingPathComponent("portrait.jpg")
        let response = try run(#"{"input":"\#(Self.portrait)","output":"\#(output.path)","mode":"preview","max_edge":600}"#)
        XCTAssertEqual(response.source.orientation, 8)
        XCTAssertEqual(response.width, 400)
        XCTAssertEqual(response.height, 600)
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(output as CFURL, nil))
        let props = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any])
        XCTAssertEqual(props[kCGImagePropertyOrientation as String] as? Int, 1)
        XCTAssertEqual(props[kCGImagePropertyPixelHeight as String] as? Int, 600)
        // 元 ARW には GPS があるが、出力には書かない。
        XCTAssertNil(props[kCGImagePropertyGPSDictionary as String])
    }

    // MARK: adjustments（CLI 0.3.0、Issue #73）

    private func adjustmentsRequest(_ input: String, _ output: URL, settings extra: String = "", _ adjustments: String) -> String {
        """
        {"input":"\(input)","output":"\(output.path)","mode":"preview","max_edge":600,"decoder_version":"9",
         "settings":{"profile":"apple_default","lens_correction":true\(extra)},"adjustments":\(adjustments)}
        """
    }

    static let identityAdjustments = #"{"exposure":0,"wb_temp":0,"shadow_lift":0,"highlight_recovery":60,"brightness":1,"contrast":1,"saturation":1,"sharpness":1,"noise_reduction":"light"}"#

    func testAdjustmentsReportDefaultsPostAndIgnored() throws {
        let input = try firstReadable(Self.highISOCandidates)
        let output = directory.appendingPathComponent("adj.jpg")
        let adjustments = #"{"exposure":0.5,"wb_temp":30,"shadow_lift":15,"highlight_recovery":60,"brightness":1.1,"contrast":1.03,"saturation":1.02,"sharpness":1.4,"noise_reduction":"full"}"#
        let response = try run(adjustmentsRequest(input, output, adjustments))
        let report = try XCTUnwrap(response.adjustments_applied)
        XCTAssertEqual(report.ignored, ["highlight_recovery"])
        XCTAssertEqual(report.guards, [])
        XCTAssertEqual(response.applied.profile, "apple_default")
        XCTAssertEqual(response.applied.exposure, 0.5, accuracy: 1e-6)
        // wb_temp > 0 は K を上げる（暖色側）。tint は既定のまま。
        XCTAssertGreaterThan(response.applied.neutral_temperature, report.defaults.neutral_temperature)
        XCTAssertEqual(response.applied.neutral_tint, report.defaults.neutral_tint, accuracy: 1e-4)
        XCTAssertEqual(report.post.wb.k0, report.defaults.neutral_temperature)
        // sharpness > 1 はデコーダ内シャープを既定のまま、増分を USM で足す。
        XCTAssertEqual(response.applied.sharpness_amount, report.defaults.sharpness_amount, accuracy: 1e-6)
        XCTAssertGreaterThan(report.post.usm.gain, 0)
        XCTAssertEqual(report.post.usm.long_edge, 600)
        // full は既定より強い NR。
        XCTAssertGreaterThan(response.applied.luminance_noise_reduction_amount, report.defaults.luminance_noise_reduction_amount)
        XCTAssertGreaterThanOrEqual(response.applied.color_noise_reduction_amount, 0.3 - 1e-6)
        XCTAssertEqual(report.post.shadow_lift, 0.15, accuracy: 1e-12)
        XCTAssertEqual(report.post.noise_reduction, "full")
        XCTAssertTrue(encodeJSONLine(response).contains("\"adjustments_applied\":{"))
    }

    func testIdentityAdjustmentsKeepDecoderDefaults() throws {
        let input = try firstReadable(Self.highISOCandidates)
        let plain = try run("""
        {"input":"\(input)","output":"\(directory.appendingPathComponent("plain.jpg").path)","mode":"preview","max_edge":600,
         "decoder_version":"9","settings":{"profile":"apple_default","lens_correction":true}}
        """)
        XCTAssertNil(plain.adjustments_applied)
        let identity = try run(adjustmentsRequest(input, directory.appendingPathComponent("identity.jpg"), Self.identityAdjustments))
        let report = try XCTUnwrap(identity.adjustments_applied)
        // 恒等の adjustments は filter に何も書かない → applied は既定リクエストと同じ。
        XCTAssertEqual(encodeJSONLine(identity.applied), encodeJSONLine(plain.applied))
        XCTAssertEqual(report.defaults.sharpness_amount, plain.applied.sharpness_amount)
        XCTAssertEqual(report.defaults.luminance_noise_reduction_amount, plain.applied.luminance_noise_reduction_amount)
        XCTAssertEqual(report.defaults.neutral_temperature, plain.applied.neutral_temperature)
        XCTAssertEqual(report.post.usm.gain, 0)
    }

    func testWhiteDotGuardFiresOnlyWithAdjustments() throws {
        let input = try firstReadable(Self.highISOCandidates)
        let overrides = #","luminance_noise_reduction":0,"sharpness_amount":0"#
        let guarded = try run(adjustmentsRequest(input, directory.appendingPathComponent("guard.jpg"), settings: overrides, Self.identityAdjustments))
        XCTAssertEqual(guarded.adjustments_applied?.guards, ["white_dot_sharpness_floor"])
        XCTAssertEqual(guarded.applied.sharpness_amount, 0.1, accuracy: 1e-6)
        XCTAssertEqual(guarded.applied.luminance_noise_reduction_amount, 0)
        // adjustments 無しの評価用リクエストには触れない（白点を意図的に再現できること）。
        let raw = try run("""
        {"input":"\(input)","output":"\(directory.appendingPathComponent("noguard.jpg").path)","mode":"preview","max_edge":600,
         "decoder_version":"9","settings":{"profile":"apple_default","lens_correction":true\(overrides)}}
        """)
        XCTAssertEqual(raw.applied.sharpness_amount, 0)
        XCTAssertNil(raw.adjustments_applied)
    }
}
