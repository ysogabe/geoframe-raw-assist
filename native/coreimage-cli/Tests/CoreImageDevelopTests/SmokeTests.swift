import CoreImage
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

    // MARK: 診断モード（CLI 0.4.0、Issue #92 設計書 §4.6 のスモーク (a)〜(f)）

    /// 明るい空のカット（設計書 §2 の事前実測と同じ DSC04705）。線形読み出しの成立 (b) などはこの画像を前提にする。
    /// 環境変数 `GEOFRAME_CI_DIAG_ARW` で別の明るいカットを指せる。
    static let highlightCandidates = [
        ProcessInfo.processInfo.environment["GEOFRAME_CI_DIAG_ARW"],
        "/Volumes/Backup/100MSDCF/DSC04705.ARW",
    ].compactMap { $0 }

    /// 0.3.0 の `applied` のキー集合（回帰スクリプトがキー集合の差も数えるので変えない。設計書 §4.1）。
    static let appliedKeys030: Set<String> = [
        "profile", "decoder_version", "scale_factor", "exposure", "baseline_exposure", "boost_amount", "boost_shadow_amount",
        "shadow_bias", "neutral_temperature", "neutral_tint", "sharpness_amount", "contrast_amount", "detail_amount",
        "luminance_noise_reduction_amount", "color_noise_reduction_amount", "moire_reduction_amount", "local_tone_map_amount",
        "extended_dynamic_range_amount", "gamut_mapping_enabled", "draft_mode_enabled", "lens_correction_enabled",
        "lens_correction_supported", "local_tone_map_supported", "orientation", "working_color_space", "color_space",
        "bit_depth", "orientation_handling",
    ]
    /// 0.3.0 の既定応答（adjustments 無し）のトップレベルのキー集合。
    static let responseKeys030: Set<String> = [
        "ok", "engine", "cli_version", "macos_version", "decoder_version", "input", "output", "mode", "format",
        "width", "height", "elapsed_ms", "applied", "source",
    ]

    private func diagRequest(
        _ input: String, _ output: URL, maxEdge: Int = 600, format: String = "jpeg", settings extra: String = "", diagnostics: String?
    ) -> String {
        let formatPart = format == "tiff16" ? #","format":"tiff16","output_color_space":"srgb""# : ""
        let diagnosticsPart = diagnostics.map { ",\"diagnostics\":\($0)" } ?? ""
        return """
        {"input":"\(input)","output":"\(output.path)","mode":"preview","max_edge":\(maxEdge)\(formatPart),"decoder_version":"9",
         "settings":{"profile":"apple_default","lens_correction":true\(extra)}\(diagnosticsPart)}
        """
    }

    private func jsonObject(_ value: some Encodable) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(encodeJSONLine(value).utf8)) as? [String: Any])
    }

    private func assertStatsEqual(_ lhs: ImageStats, _ rhs: ImageStats, accuracy: Double, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(lhs.width, rhs.width, file: file, line: line)
        XCTAssertEqual(lhs.height, rhs.height, file: file, line: line)
        for (a, b) in [(lhs.r, rhs.r), (lhs.g, rhs.g), (lhs.b, rhs.b)] {
            for (x, y) in [(a.mean, b.mean), (a.p50, b.p50), (a.p99, b.p99), (a.p999, b.p999), (a.max, b.max), (a.min, b.min),
                           (a.frac_gt_1, b.frac_gt_1), (a.frac_ge_1, b.frac_ge_1), (a.frac_lt_0, b.frac_lt_0)] {
                XCTAssertEqual(x, y, accuracy: accuracy, file: file, line: line)
            }
        }
        XCTAssertEqual(lhs.any_gt_1, rhs.any_gt_1, accuracy: accuracy, file: file, line: line)
        XCTAssertEqual(lhs.all_gt_1, rhs.all_gt_1, accuracy: accuracy, file: file, line: line)
    }

    /// (a) `diagnostics` 無しと `diagnostics: {}` で tiff16 のバイト列が一致し、既定応答の形は 0.3.0 のまま。
    func testDiagnosticsAbsentVersusEmptyWriteIdenticalTIFF() throws {
        let input = try firstReadable(Self.highlightCandidates + Self.highISOCandidates)
        let plainURL = directory.appendingPathComponent("plain.tif")
        let diagURL = directory.appendingPathComponent("diag.tif")
        let plain = try run(diagRequest(input, plainURL, format: "tiff16", diagnostics: nil))
        let diag = try run(diagRequest(input, diagURL, format: "tiff16", diagnostics: "{}"))
        XCTAssertEqual(try Data(contentsOf: plainURL), try Data(contentsOf: diagURL), "tiff16 bytes differ between no diagnostics and {}")
        XCTAssertEqual(encodeJSONLine(plain.applied), encodeJSONLine(diag.applied))

        let plainObject = try jsonObject(plain)
        XCTAssertEqual(Set(plainObject.keys), Self.responseKeys030)
        XCTAssertEqual(Set(try XCTUnwrap(plainObject["applied"] as? [String: Any]).keys), Self.appliedKeys030)
        XCTAssertNil(plain.diagnostics)
        let diagObject = try jsonObject(diag)
        XCTAssertEqual(Set(diagObject.keys), Self.responseKeys030.union(["diagnostics"]))
        XCTAssertEqual(Set(try XCTUnwrap(diagObject["applied"] as? [String: Any]).keys), Self.appliedKeys030)
        let report = try XCTUnwrap(diag.diagnostics)
        XCTAssertTrue(report.written)
        XCTAssertNotNil(report.stats)
        XCTAssertNil(report.dump_float)
        XCTAssertEqual(report.stats_color_space, "linear_srgb")
    }

    /// (b) `boost_amount 0` + `gamut_mapping false` で表示白（1.0）を超える値が読める（線形読み出し S1）。
    func testLinearReadoutExceedsDisplayWhite() throws {
        let input = try firstReadable(Self.highlightCandidates)
        let response = try run(diagRequest(
            input, directory.appendingPathComponent("s1.jpg"), settings: #","boost_amount":0,"gamut_mapping":false"#,
            diagnostics: #"{"skip_write":true}"#
        ))
        XCTAssertFalse(response.applied.gamut_mapping_enabled)
        XCTAssertEqual(response.applied.boost_amount, 0)
        let report = try XCTUnwrap(response.diagnostics)
        XCTAssertFalse(report.readback.gamut_mapping_enabled)
        let stats = try XCTUnwrap(report.stats)
        XCTAssertGreaterThan(stats.any_gt_1, 0)
        XCTAssertGreaterThan(max(stats.r.max, stats.g.max, stats.b.max), 1.5)
    }

    /// (c) `baseline_exposure 0` と `exposure_ev −(ファイルの baselineExposure)` の統計が一致する（§2.5）。
    func testBaselineExposureZeroMatchesNegativeExposure() throws {
        let input = try firstReadable(Self.highlightCandidates)
        let reference = try run(diagRequest(input, directory.appendingPathComponent("ref.jpg"), diagnostics: #"{"skip_write":true}"#))
        let baseline = reference.applied.baseline_exposure
        XCTAssertNotEqual(baseline, 0, "this check needs a file whose baselineExposure is not 0")
        let zeroed = try run(diagRequest(
            input, directory.appendingPathComponent("b0.jpg"), settings: #","baseline_exposure":0"#, diagnostics: #"{"skip_write":true}"#
        ))
        XCTAssertEqual(zeroed.applied.baseline_exposure, 0)
        XCTAssertEqual(zeroed.diagnostics?.readback.baseline_exposure, 0)
        let shifted = try run(diagRequest(
            input, directory.appendingPathComponent("ev.jpg"), settings: ",\"exposure_ev\":\(-baseline)", diagnostics: #"{"skip_write":true}"#
        ))
        assertStatsEqual(try XCTUnwrap(zeroed.diagnostics?.stats), try XCTUnwrap(shifted.diagnostics?.stats), accuracy: 1e-6)
    }

    /// (d) `linear_space_filter identity` の統計は既定と一致する。scale 0.5 は exposure −1 相当（§2.4）。
    func testIdentityLinearSpaceFilterMatchesDefault() throws {
        let input = try firstReadable(Self.highlightCandidates)
        let plain = try run(diagRequest(input, directory.appendingPathComponent("p.jpg"), diagnostics: #"{"skip_write":true}"#))
        let identity = try run(diagRequest(
            input, directory.appendingPathComponent("i.jpg"), diagnostics: #"{"skip_write":true,"linear_space_filter":{"kind":"identity"}}"#
        ))
        assertStatsEqual(try XCTUnwrap(plain.diagnostics?.stats), try XCTUnwrap(identity.diagnostics?.stats), accuracy: 1e-6)
        XCTAssertEqual(encodeJSONLine(try XCTUnwrap(identity.diagnostics?.linear_space_filter)), #"{"filter":"CIColorMatrix","kind":"identity","value":null}"#)

        let half = try run(diagRequest(
            input, directory.appendingPathComponent("h.jpg"), diagnostics: #"{"skip_write":true,"linear_space_filter":{"kind":"scale","value":0.5}}"#
        ))
        let ev = try run(diagRequest(
            input, directory.appendingPathComponent("e.jpg"), settings: #","exposure_ev":-1"#, diagnostics: #"{"skip_write":true}"#
        ))
        XCTAssertEqual(try XCTUnwrap(half.diagnostics?.stats).g.mean, try XCTUnwrap(ev.diagnostics?.stats).g.mean, accuracy: 2e-3)
    }

    /// (e) `readback.extent` が scaleFactor から期待される寸法（outputImage の二重参照で縮んでいない）。
    /// response.width も同じ extent から出るので、期待値は native 寸法と scale_factor から独立に計算する。
    func testReadbackExtentMatchesScaleFactor() throws {
        let input = try firstReadable(Self.highlightCandidates + Self.highISOCandidates)
        let response = try run(diagRequest(
            input, directory.appendingPathComponent("x.jpg"), maxEdge: 1200,
            diagnostics: #"{"linear_space_filter":{"kind":"identity"},"post_filters":[{"name":"CIExposureAdjust","inputEV":0}]}"#
        ))
        let report = try XCTUnwrap(response.diagnostics)
        let scale = response.applied.scale_factor
        var expectedW = (Double(response.source.native_width) * scale).rounded()
        var expectedH = (Double(response.source.native_height) * scale).rounded()
        if (5...8).contains(response.source.orientation) { swap(&expectedW, &expectedH) }
        XCTAssertEqual(max(expectedW, expectedH), 1200)
        XCTAssertEqual(report.readback.extent, [0, 0, expectedW, expectedH])
        XCTAssertEqual(Double(response.width), expectedW)
        XCTAssertEqual(Double(response.height), expectedH)
        XCTAssertEqual(Double(try XCTUnwrap(report.stats).width), expectedW)
    }

    /// (e) の検出力の確認: 同じ filter で outputImage を 2 回参照すると extent が縮む（設計書 §2.2 の罠）。
    /// OS がこの挙動を直していたら skip（CLI は 1 回しか参照しないので影響は無い）。
    func testDoubleReferenceOfOutputImageShrinksExtent() throws {
        let input = try firstReadable(Self.highlightCandidates + Self.highISOCandidates)
        let filter = try XCTUnwrap(CIRAWFilter(imageURL: URL(fileURLWithPath: input)))
        filter.decoderVersion = CIRAWDecoderVersion(rawValue: "9")
        filter.scaleFactor = 0.2
        let first = try XCTUnwrap(filter.outputImage).extent
        let second = try XCTUnwrap(filter.outputImage).extent
        guard second != first else {
            throw XCTSkip("outputImage double reference no longer shrinks the extent on this OS (\(first))")
        }
        XCTAssertLessThan(second.width, first.width)
    }

    /// (f) `skip_write` では output を作らない（パス検証は従来どおり）。
    func testSkipWriteCreatesNoOutput() throws {
        let input = try firstReadable(Self.highlightCandidates + Self.highISOCandidates)
        let output = directory.appendingPathComponent("skipped.tif")
        let response = try run(diagRequest(input, output, format: "tiff16", diagnostics: #"{"skip_write":true,"stats":false}"#))
        XCTAssertFalse(FileManager.default.fileExists(atPath: output.path))
        let report = try XCTUnwrap(response.diagnostics)
        XCTAssertFalse(report.written)
        XCTAssertNil(report.stats)
        XCTAssertEqual(response.output, output.path)
        XCTAssertEqual(Double(response.width), report.readback.extent[2])
        XCTAssertEqual(Double(response.height), report.readback.extent[3])
        // 出力ディレクトリが無ければ skip_write でも OUTPUT_DIR_NOT_FOUND。
        XCTAssertThrowsError(try run(diagRequest(
            input, directory.appendingPathComponent("missing/o.jpg"), diagnostics: #"{"skip_write":true}"#
        ))) { XCTAssertEqual(($0 as? DevelopError)?.code, .outputDirNotFound) }
    }

    /// float dump: 寸法・バイト数・中身（統計の max と一致）。
    func testDumpFloatMatchesStats() throws {
        let input = try firstReadable(Self.highlightCandidates + Self.highISOCandidates)
        let dumpURL = directory.appendingPathComponent("s1.f32")
        let response = try run(diagRequest(
            input, directory.appendingPathComponent("d.jpg"), maxEdge: 300, settings: #","boost_amount":0,"gamut_mapping":false"#,
            diagnostics: "{\"skip_write\":true,\"dump_float\":{\"path\":\"\(dumpURL.path)\"}}"
        ))
        let report = try XCTUnwrap(response.diagnostics)
        let dump = try XCTUnwrap(report.dump_float)
        let stats = try XCTUnwrap(report.stats)
        XCTAssertEqual(dump.path, dumpURL.path)
        XCTAssertEqual(dump.width, response.width)
        XCTAssertEqual(dump.height, response.height)
        XCTAssertEqual(dump.color_space, "linear_srgb")
        let data = try Data(contentsOf: dumpURL)
        XCTAssertEqual(data.count, dump.width * dump.height * 3 * 4)
        XCTAssertEqual(dump.bytes, data.count)
        var maxima: [Float] = [-.infinity, -.infinity, -.infinity]
        data.withUnsafeBytes { raw in
            for i in 0..<(dump.width * dump.height * 3) {
                let v = Float(bitPattern: UInt32(littleEndian: raw.loadUnaligned(fromByteOffset: i * 4, as: UInt32.self)))
                maxima[i % 3] = max(maxima[i % 3], v)
            }
        }
        XCTAssertEqual(Double(maxima[0]), stats.r.max, accuracy: 1e-6)
        XCTAssertEqual(Double(maxima[1]), stats.g.max, accuracy: 1e-6)
        XCTAssertEqual(Double(maxima[2]), stats.b.max, accuracy: 1e-6)
        // 一時ファイルが残っていない。
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: directory.path).filter { $0.hasSuffix(".tmp") }
        XCTAssertEqual(leftovers, [])
    }

    /// post_filters は outputImage の直後（clamp 前）に掛かる: CIExposureAdjust −1 で線形読み出しの max が半分。
    func testPostFiltersRunBeforeClamp() throws {
        let input = try firstReadable(Self.highlightCandidates)
        let settings = #","boost_amount":0,"gamut_mapping":false"#
        let plain = try run(diagRequest(input, directory.appendingPathComponent("a.jpg"), settings: settings, diagnostics: #"{"skip_write":true}"#))
        let halved = try run(diagRequest(
            input, directory.appendingPathComponent("b.jpg"), settings: settings,
            diagnostics: #"{"skip_write":true,"post_filters":[{"name":"CIExposureAdjust","inputEV":-1}]}"#
        ))
        let a = try XCTUnwrap(plain.diagnostics?.stats)
        let b = try XCTUnwrap(halved.diagnostics?.stats)
        XCTAssertGreaterThan(a.g.max, 1.5)
        XCTAssertEqual(b.g.max, a.g.max / 2, accuracy: a.g.max * 0.01)
        XCTAssertEqual(halved.diagnostics?.post_filters?.first?.applied_parameters["inputEV"], .number(-1))
    }
}
