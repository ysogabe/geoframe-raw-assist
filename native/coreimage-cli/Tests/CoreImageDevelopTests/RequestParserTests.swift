import CoreImage
import Foundation
import ImageIO
import XCTest

@testable import CoreImageDevelop

final class RequestParserTests: XCTestCase {
    private func parse(_ json: String) throws -> DevelopRequest {
        try RequestParser.parse(Data(json.utf8))
    }

    private func assertInvalid(_ json: String, contains fragment: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertThrowsError(try parse(json), file: file, line: line) { error in
            guard let error = error as? DevelopError else {
                return XCTFail("unexpected error type: \(error)", file: file, line: line)
            }
            XCTAssertEqual(error.code, .invalidRequest, file: file, line: line)
            XCTAssertTrue(error.message.contains(fragment), "message: \(error.message)", file: file, line: line)
        }
    }

    func testMinimalPreviewRequestFillsDefaults() throws {
        let request = try parse(#"{"input":"/a/DSC00001.ARW","output":"/b/out.jpg","mode":"preview"}"#)
        XCTAssertEqual(request.input.path, "/a/DSC00001.ARW")
        XCTAssertEqual(request.output.path, "/b/out.jpg")
        XCTAssertEqual(request.mode, .preview)
        XCTAssertEqual(request.maxEdge, 2400)
        XCTAssertEqual(request.format, .jpeg)
        XCTAssertEqual(request.jpegQuality, 0.92)
        XCTAssertNil(request.decoderVersion)
        XCTAssertEqual(request.settings, Settings())
        XCTAssertEqual(request.settings.profile, .neutral)
        XCTAssertFalse(request.settings.lensCorrection)
        XCTAssertNil(request.outputColorSpace)
        XCTAssertNil(request.settings.luminanceNoiseReduction)
    }

    func testFullRequestWithAllFields() throws {
        let request = try parse("""
        {"input":"/a/x.arw","output":"/b/out.TIFF","mode":"full","format":"tiff16","decoder_version":"8",
         "settings":{"exposure_ev":-0.5,"neutral_temperature":5200,"neutral_tint":-3,"lens_correction":true,"profile":"apple_default"}}
        """)
        XCTAssertEqual(request.mode, .full)
        XCTAssertNil(request.maxEdge)
        XCTAssertEqual(request.format, .tiff16)
        XCTAssertNil(request.jpegQuality)
        XCTAssertEqual(request.decoderVersion, "8")
        XCTAssertEqual(request.settings.exposureEV, -0.5)
        XCTAssertEqual(request.settings.neutralTemperature, 5200)
        XCTAssertEqual(request.settings.neutralTint, -3)
        XCTAssertTrue(request.settings.lensCorrection)
        XCTAssertEqual(request.settings.profile, .appleDefault)
        // tiff16 で output_color_space を省くと互換のため Display P3。
        XCTAssertEqual(request.outputColorSpace, .displayP3)
    }

    func testOutputColorSpaceValidation() throws {
        let tiff = #""input":"/a/x.ARW","output":"/b/o.tif","mode":"full","format":"tiff16""#
        XCTAssertEqual(try parse("{\(tiff),\"output_color_space\":\"srgb\"}").outputColorSpace, .srgb)
        XCTAssertEqual(try parse("{\(tiff),\"output_color_space\":\"display_p3\"}").outputColorSpace, .displayP3)
        XCTAssertEqual(try parse("{\(tiff),\"output_color_space\":null}").outputColorSpace, .displayP3)
        assertInvalid("{\(tiff),\"output_color_space\":\"adobe_rgb\"}", contains: "output_color_space")
        assertInvalid("{\(tiff),\"output_color_space\":1}", contains: "output_color_space")
        // jpeg は常に sRGB なので指定自体を拒否する（null は省略と同じで通る）。
        let jpeg = #""input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview""#
        assertInvalid("{\(jpeg),\"output_color_space\":\"srgb\"}", contains: "only allowed with format \"tiff16\"")
        XCTAssertNil(try parse("{\(jpeg),\"output_color_space\":null}").outputColorSpace)
    }

    func testSettingsOverridesAreParsedAndRangeChecked() throws {
        let base = #""input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview""#
        let request = try parse("""
        {\(base),"settings":{"profile":"apple_default","luminance_noise_reduction":0,"color_noise_reduction":0.5,
         "sharpness_amount":1,"detail_amount":2.5,"extended_dynamic_range":1.5,"boost_amount":0.8,"boost_shadow_amount":1.2}}
        """)
        XCTAssertEqual(request.settings.luminanceNoiseReduction, 0)
        XCTAssertEqual(request.settings.colorNoiseReduction, 0.5)
        XCTAssertEqual(request.settings.sharpnessAmount, 1)
        XCTAssertEqual(request.settings.detailAmount, 2.5)
        XCTAssertEqual(request.settings.extendedDynamicRange, 1.5)
        XCTAssertEqual(request.settings.boostAmount, 0.8)
        XCTAssertEqual(request.settings.boostShadowAmount, 1.2)
        // null は「profile の既定値のまま」= nil。
        let nulls = try parse("{\(base),\"settings\":{\"luminance_noise_reduction\":null,\"boost_amount\":null}}")
        XCTAssertNil(nulls.settings.luminanceNoiseReduction)
        XCTAssertNil(nulls.settings.boostAmount)
        XCTAssertEqual(nulls.settings, Settings())
        // 値域（CIRAWFilter.h のコメントどおり）と型。
        assertInvalid("{\(base),\"settings\":{\"luminance_noise_reduction\":1.1}}", contains: "luminance_noise_reduction")
        assertInvalid("{\(base),\"settings\":{\"color_noise_reduction\":-0.1}}", contains: "color_noise_reduction")
        assertInvalid("{\(base),\"settings\":{\"sharpness_amount\":2}}", contains: "sharpness_amount")
        assertInvalid("{\(base),\"settings\":{\"detail_amount\":3.5}}", contains: "detail_amount")
        assertInvalid("{\(base),\"settings\":{\"extended_dynamic_range\":2.5}}", contains: "extended_dynamic_range")
        assertInvalid("{\(base),\"settings\":{\"boost_amount\":1.5}}", contains: "boost_amount")
        assertInvalid("{\(base),\"settings\":{\"boost_shadow_amount\":3}}", contains: "boost_shadow_amount")
        assertInvalid("{\(base),\"settings\":{\"sharpness_amount\":\"1\"}}", contains: "sharpness_amount")
        assertInvalid("{\(base),\"settings\":{\"sharpness_amount\":true}}", contains: "sharpness_amount")
    }

    /// 診断用の settings 2 キー（CLI 0.4.0、Issue #92 設計書 §4.2）。#79 の個別上書きと同じ規則。
    func testDiagnosticSettingsGamutMappingAndBaselineExposure() throws {
        let base = #""input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview""#
        let request = try parse("{\(base),\"settings\":{\"gamut_mapping\":false,\"baseline_exposure\":0}}")
        XCTAssertEqual(request.settings.gamutMapping, false)
        XCTAssertEqual(request.settings.baselineExposure, 0)
        XCTAssertEqual(try parse("{\(base),\"settings\":{\"gamut_mapping\":true,\"baseline_exposure\":-5}}").settings.baselineExposure, -5)
        // 省略・null は CIRAWFilter の既定のまま（既定リクエストと同じ Settings）。
        let nulls = try parse("{\(base),\"settings\":{\"gamut_mapping\":null,\"baseline_exposure\":null}}")
        XCTAssertEqual(nulls.settings, Settings())
        XCTAssertNil(try parse("{\(base)}").settings.gamutMapping)
        assertInvalid("{\(base),\"settings\":{\"gamut_mapping\":0}}", contains: "settings.gamut_mapping")
        assertInvalid("{\(base),\"settings\":{\"gamut_mapping\":\"false\"}}", contains: "settings.gamut_mapping")
        assertInvalid("{\(base),\"settings\":{\"baseline_exposure\":5.5}}", contains: "settings.baseline_exposure")
        assertInvalid("{\(base),\"settings\":{\"baseline_exposure\":true}}", contains: "settings.baseline_exposure")
        // adjustments との同時指定は可（§4.2）。
        let adj = #"{"exposure":0,"wb_temp":0,"shadow_lift":0,"highlight_recovery":60,"brightness":1,"contrast":1,"saturation":1,"sharpness":1,"noise_reduction":"light"}"#
        let both = try parse("{\(base),\"settings\":{\"profile\":\"apple_default\",\"gamut_mapping\":false,\"baseline_exposure\":0.2},\"adjustments\":\(adj)}")
        XCTAssertEqual(both.settings.gamutMapping, false)
        XCTAssertNotNil(both.adjustments)
    }

    func testNullOptionalFieldsUseDefaults() throws {
        let request = try parse("""
        {"input":"/a/x.ARW","output":"/b/o.jpeg","mode":"preview","max_edge":null,"format":null,"jpeg_quality":null,
         "decoder_version":null,"settings":{"neutral_temperature":null,"neutral_tint":null}}
        """)
        XCTAssertEqual(request.maxEdge, 2400)
        XCTAssertEqual(request.jpegQuality, 0.92)
        XCTAssertNil(request.settings.neutralTemperature)
    }

    func testMalformedJSON() {
        assertInvalid("not json", contains: "not valid JSON")
        assertInvalid("[1,2]", contains: "JSON object")
        assertInvalid("", contains: "not valid JSON")
    }

    func testUnknownKeysAreRejected() {
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","extra":1}"#, contains: "unknown request key(s): extra")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","settings":{"sharpness":1}}"#, contains: "unknown settings key(s): sharpness")
    }

    func testPathsMustBeAbsoluteStrings() {
        assertInvalid(#"{"output":"/b/o.jpg","mode":"preview"}"#, contains: "input must be a non-empty string")
        assertInvalid(#"{"input":"","output":"/b/o.jpg","mode":"preview"}"#, contains: "input must be a non-empty string")
        assertInvalid(#"{"input":"a/x.ARW","output":"/b/o.jpg","mode":"preview"}"#, contains: "input must be an absolute path")
        assertInvalid(#"{"input":"/a/x.ARW","output":"o.jpg","mode":"preview"}"#, contains: "output must be an absolute path")
        assertInvalid(#"{"input":1,"output":"/b/o.jpg","mode":"preview"}"#, contains: "input must be a non-empty string")
    }

    func testInputMustBeARWCaseInsensitive() throws {
        XCTAssertNoThrow(try parse(#"{"input":"/a/x.Arw","output":"/b/o.jpg","mode":"preview"}"#))
        assertInvalid(#"{"input":"/a/x.NEF","output":"/b/o.jpg","mode":"preview"}"#, contains: ".ARW")
        assertInvalid(#"{"input":"/a/x","output":"/b/o.jpg","mode":"preview"}"#, contains: ".ARW")
    }

    func testModeValidation() {
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg"}"#, contains: "mode")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"draft"}"#, contains: "mode")
    }

    func testMaxEdgeValidation() throws {
        XCTAssertEqual(try parse(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","max_edge":1600}"#).maxEdge, 1600)
        XCTAssertEqual(try parse(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","max_edge":1600.0}"#).maxEdge, 1600)
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","max_edge":10}"#, contains: "max_edge")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","max_edge":1600.5}"#, contains: "max_edge")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","max_edge":"1600"}"#, contains: "max_edge")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","max_edge":true}"#, contains: "max_edge")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"full","max_edge":2400}"#, contains: "only allowed with mode \"preview\"")
    }

    func testFormatAndExtensionMustAgree() {
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","format":"png"}"#, contains: "format")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.tif","mode":"preview"}"#, contains: ".jpg or .jpeg")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","format":"tiff16"}"#, contains: ".tif or .tiff")
    }

    func testJPEGQualityValidation() throws {
        XCTAssertEqual(try parse(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","jpeg_quality":1}"#).jpegQuality, 1.0)
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","jpeg_quality":1.5}"#, contains: "jpeg_quality")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","jpeg_quality":-0.1}"#, contains: "jpeg_quality")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.tif","mode":"preview","format":"tiff16","jpeg_quality":0.9}"#, contains: "only allowed with format \"jpeg\"")
    }

    func testDecoderVersionValidation() throws {
        XCTAssertEqual(try parse(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","decoder_version":"9"}"#).decoderVersion, "9")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","decoder_version":"7"}"#, contains: "decoder_version")
        assertInvalid(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","decoder_version":8}"#, contains: "decoder_version")
    }

    func testSettingsValidation() {
        let base = #""input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview""#
        assertInvalid("{\(base),\"settings\":[]}", contains: "settings must be a JSON object")
        assertInvalid("{\(base),\"settings\":{\"exposure_ev\":6}}", contains: "exposure_ev")
        assertInvalid("{\(base),\"settings\":{\"exposure_ev\":\"1\"}}", contains: "exposure_ev")
        assertInvalid("{\(base),\"settings\":{\"exposure_ev\":true}}", contains: "exposure_ev")
        assertInvalid("{\(base),\"settings\":{\"neutral_temperature\":1000}}", contains: "neutral_temperature")
        assertInvalid("{\(base),\"settings\":{\"neutral_tint\":200}}", contains: "neutral_tint")
        assertInvalid("{\(base),\"settings\":{\"lens_correction\":1}}", contains: "lens_correction")
        assertInvalid("{\(base),\"settings\":{\"lens_correction\":\"true\"}}", contains: "lens_correction")
        assertInvalid("{\(base),\"settings\":{\"profile\":\"vivid\"}}", contains: "profile")
    }
}

final class PathValidatorTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("ci-develop-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private func request(input: URL, output: URL) throws -> DevelopRequest {
        try RequestParser.parse(Data(#"{"input":"\#(input.path)","output":"\#(output.path)","mode":"preview"}"#.utf8))
    }

    private func assertCode(_ code: ErrorCode, _ body: () throws -> Void, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertThrowsError(try body(), file: file, line: line) { error in
            XCTAssertEqual((error as? DevelopError)?.code, code, "\(error)", file: file, line: line)
        }
    }

    func testMissingInput() throws {
        let req = try request(input: directory.appendingPathComponent("none.ARW"), output: directory.appendingPathComponent("o.jpg"))
        assertCode(.inputNotFound) { try PathValidator.validate(req) }
    }

    func testInputDirectoryIsRejected() throws {
        let dirInput = directory.appendingPathComponent("folder.ARW")
        try FileManager.default.createDirectory(at: dirInput, withIntermediateDirectories: false)
        let req = try request(input: dirInput, output: directory.appendingPathComponent("o.jpg"))
        assertCode(.invalidRequest) { try PathValidator.validate(req) }
    }

    func testUnreadableInput() throws {
        let input = directory.appendingPathComponent("locked.ARW")
        FileManager.default.createFile(atPath: input.path, contents: Data([0]), attributes: [.posixPermissions: 0o000])
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: input.path) }
        let req = try request(input: input, output: directory.appendingPathComponent("o.jpg"))
        assertCode(.inputNotReadable) { try PathValidator.validate(req) }
    }

    func testMissingOutputDirectory() throws {
        let input = directory.appendingPathComponent("x.ARW")
        FileManager.default.createFile(atPath: input.path, contents: Data([0]))
        let req = try request(input: input, output: directory.appendingPathComponent("missing/o.jpg"))
        assertCode(.outputDirNotFound) { try PathValidator.validate(req) }
    }

    func testOutputSymlinkToInputIsRejected() throws {
        let input = directory.appendingPathComponent("x.ARW")
        FileManager.default.createFile(atPath: input.path, contents: Data([0]))
        let link = directory.appendingPathComponent("link.jpg")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: input)
        let req = try request(input: input, output: link)
        assertCode(.invalidRequest) { try PathValidator.validate(req) }
    }

    func testOutputHardLinkToInputIsRejected() throws {
        let input = directory.appendingPathComponent("x.ARW")
        FileManager.default.createFile(atPath: input.path, contents: Data([0]))
        let hard = directory.appendingPathComponent("hard.jpg")
        try FileManager.default.linkItem(at: input, to: hard)
        let req = try request(input: input, output: hard)
        assertCode(.invalidRequest) { try PathValidator.validate(req) }
    }

    func testValidPathsPass() throws {
        let input = directory.appendingPathComponent("x.ARW")
        FileManager.default.createFile(atPath: input.path, contents: Data([0]))
        let req = try request(input: input, output: directory.appendingPathComponent("o.jpg"))
        XCTAssertNoThrow(try PathValidator.validate(req))
    }
}

final class WriterTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("ci-writer-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private func sampleImage() -> CGImage {
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        let context = CGContext(data: nil, width: 8, height: 4, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.setFillColor(red: 1, green: 0.5, blue: 0, alpha: 1)
        context.fill(CGRect(x: 0, y: 0, width: 8, height: 4))
        return context.makeImage()!
    }

    private func leftovers() throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: directory.path).filter { $0.hasSuffix(".tmp") }
    }

    func testJPEGGetsSRGBICCProfileAndNoGPS() throws {
        let output = directory.appendingPathComponent("o.jpg")
        try AtomicImageWriter.write(sampleImage(), to: output, format: .jpeg, jpegQuality: 0.9)
        let data = try Data(contentsOf: output)
        XCTAssertNotNil(data.range(of: Data("ICC_PROFILE\0".utf8)))
        let source = try XCTUnwrap(CGImageSourceCreateWithData(data as CFData, nil))
        let props = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any])
        XCTAssertEqual(props[kCGImagePropertyProfileName as String] as? String, "sRGB IEC61966-2.1")
        XCTAssertNil(props[kCGImagePropertyGPSDictionary as String])
        XCTAssertEqual(props[kCGImagePropertyPixelWidth as String] as? Int, 8)
        XCTAssertEqual(try leftovers(), [])
    }

    func testEmbedIsIdempotentAndRejectsNonJPEG() throws {
        let icc = try XCTUnwrap(CGColorSpace(name: CGColorSpace.sRGB)?.copyICCData() as Data?)
        let jpeg = try AtomicImageWriter.encode(sampleImage(), format: .jpeg, jpegQuality: 0.9)
        let once = try JPEGICC.embed(icc, into: jpeg)
        let twice = try JPEGICC.embed(icc, into: once)
        XCTAssertEqual(once, twice)
        XCTAssertEqual(once.count, jpeg.count + 4 + 12 + 2 + icc.count)
        XCTAssertThrowsError(try JPEGICC.embed(icc, into: Data([0x00, 0x01, 0x02, 0x03])))
    }

    func testTIFF16IsSixteenBitWithoutAlpha() throws {
        let output = directory.appendingPathComponent("o.tif")
        let image = CIImage(color: CIColor(red: 0.2, green: 0.4, blue: 0.6)).cropped(to: CGRect(x: 0, y: 0, width: 6, height: 4))
        let space = CGColorSpace(name: CGColorSpace.displayP3)!
        let cg = try renderRGB16(image, extent: image.extent, colorSpace: space, context: CIContext())
        try AtomicImageWriter.write(cg, to: output, format: .tiff16, jpegQuality: nil)
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(output as CFURL, nil))
        let props = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any])
        XCTAssertEqual(props[kCGImagePropertyDepth as String] as? Int, 16)
        XCTAssertNotEqual(props[kCGImagePropertyHasAlpha as String] as? Bool, true)
        XCTAssertEqual(props[kCGImagePropertyProfileName as String] as? String, "Display P3")
    }

    func testTIFF16CanBeWrittenAsSRGB() throws {
        let output = directory.appendingPathComponent("srgb.tif")
        let image = CIImage(color: CIColor(red: 0.2, green: 0.4, blue: 0.6)).cropped(to: CGRect(x: 0, y: 0, width: 6, height: 4))
        let (space, name) = OutputColor.space(for: .tiff16, colorSpace: .srgb)
        XCTAssertEqual(name, "sRGB IEC61966-2.1")
        let cg = try renderRGB16(image, extent: image.extent, colorSpace: space, context: CIContext())
        try AtomicImageWriter.write(cg, to: output, format: .tiff16, jpegQuality: nil)
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(output as CFURL, nil))
        let props = try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any])
        XCTAssertEqual(props[kCGImagePropertyDepth as String] as? Int, 16)
        XCTAssertEqual(props[kCGImagePropertyProfileName as String] as? String, "sRGB IEC61966-2.1")
        // 既定（nil）と display_p3 は従来どおり P3、jpeg は指定に関わらず sRGB。
        XCTAssertEqual(OutputColor.space(for: .tiff16, colorSpace: nil).1, "Display P3")
        XCTAssertEqual(OutputColor.space(for: .tiff16, colorSpace: .displayP3).1, "Display P3")
        XCTAssertEqual(OutputColor.space(for: .jpeg, colorSpace: nil).1, "sRGB IEC61966-2.1")
    }

    func testOverwriteReplacesExistingOutput() throws {
        let output = directory.appendingPathComponent("o.jpg")
        try Data("old".utf8).write(to: output)
        try AtomicImageWriter.writeAtomically(Data("new".utf8), to: output)
        XCTAssertEqual(try String(contentsOf: output, encoding: .utf8), "new")
        XCTAssertEqual(try leftovers(), [])
    }

    func testFailedRenameKeepsExistingOutputAndRemovesTemp() throws {
        // output がディレクトリ（中身あり）だと rename(2) は失敗する → 既存は無傷・一時ファイルも残らない。
        let output = directory.appendingPathComponent("busy.jpg")
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: false)
        try Data("keep".utf8).write(to: output.appendingPathComponent("inner"))
        XCTAssertThrowsError(try AtomicImageWriter.writeAtomically(Data("new".utf8), to: output)) { error in
            XCTAssertEqual((error as? DevelopError)?.code, .writeFailed)
        }
        XCTAssertEqual(try String(contentsOf: output.appendingPathComponent("inner"), encoding: .utf8), "keep")
        XCTAssertEqual(try leftovers(), [])
    }

    func testMissingDirectoryFailsWithoutLeftovers() {
        let output = directory.appendingPathComponent("nope/o.jpg")
        XCTAssertThrowsError(try AtomicImageWriter.writeAtomically(Data("x".utf8), to: output)) { error in
            XCTAssertEqual((error as? DevelopError)?.code, .writeFailed)
        }
    }

    func testFailureResponseEncoding() {
        let line = encodeJSONLine(FailureResponse(DevelopError(.inputNotFound, "input does not exist: /a/b.ARW")))
        XCTAssertEqual(line, #"{"error":{"code":"INPUT_NOT_FOUND","message":"input does not exist: /a/b.ARW"},"ok":false}"#)
        XCTAssertFalse(line.contains("\n"))
    }
}

/// `adjustments`（CLI 0.3.0、設計書 §4.1）の解析・値域・排他。
final class AdjustmentsRequestParserTests: XCTestCase {
    static let base = #""input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","decoder_version":"9""#
    static let appleDefault = #""settings":{"profile":"apple_default","lens_correction":true}"#
    static let fullAdjustments: [String: String] = [
        "exposure": "0.5", "wb_temp": "-20", "shadow_lift": "15", "highlight_recovery": "60", "brightness": "1.1",
        "contrast": "1.03", "saturation": "1.02", "sharpness": "1.04", "noise_reduction": "\"full\"",
    ]

    private func adjustmentsJSON(_ overrides: [String: String?] = [:]) -> String {
        var entries = Self.fullAdjustments
        for (key, value) in overrides {
            entries[key] = value
        }
        let body = entries.keys.sorted().map { "\"\($0)\":\(entries[$0]!)" }.joined(separator: ",")
        return "{\(body)}"
    }

    private func request(adjustments: String, settings: String = appleDefault) -> String {
        "{\(Self.base),\(settings),\"adjustments\":\(adjustments)}"
    }

    private func parse(_ json: String) throws -> DevelopRequest {
        try RequestParser.parse(Data(json.utf8))
    }

    private func assertInvalid(_ json: String, contains fragment: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertThrowsError(try parse(json), file: file, line: line) { error in
            XCTAssertEqual((error as? DevelopError)?.code, .invalidRequest, file: file, line: line)
            let message = (error as? DevelopError)?.message ?? ""
            XCTAssertTrue(message.contains(fragment), "message: \(message)", file: file, line: line)
        }
    }

    func testFullAdjustmentsAreParsed() throws {
        let request = try parse(request(adjustments: adjustmentsJSON()))
        let adjustments = try XCTUnwrap(request.adjustments)
        XCTAssertEqual(adjustments, Adjustments(
            exposure: 0.5, wbTemp: -20, shadowLift: 15, highlightRecovery: 60, brightness: 1.1,
            contrast: 1.03, saturation: 1.02, sharpness: 1.04, noiseReduction: .full
        ))
        XCTAssertEqual(request.settings.profile, .appleDefault)
    }

    func testAdjustmentsAreOptionalAndNullMeansOmitted() throws {
        XCTAssertNil(try parse("{\(Self.base),\(Self.appleDefault)}").adjustments)
        XCTAssertNil(try parse("{\(Self.base),\(Self.appleDefault),\"adjustments\":null}").adjustments)
        // 既定リクエスト（adjustments 無し）は neutral のままでよい（0.2.0 と同じ）。
        XCTAssertNil(try parse("{\(Self.base)}").adjustments)
    }

    func testAllNineKeysAreRequired() {
        for key in Self.fullAdjustments.keys {
            var entries = Self.fullAdjustments
            entries.removeValue(forKey: key)
            let body = entries.keys.sorted().map { "\"\($0)\":\(entries[$0]!)" }.joined(separator: ",")
            assertInvalid(request(adjustments: "{\(body)}"), contains: "missing: \(key)")
            assertInvalid(request(adjustments: adjustmentsJSON([key: "null"])), contains: "missing: \(key)")
        }
        assertInvalid(request(adjustments: "{}"), contains: "requires all 9 keys")
    }

    func testUnknownKeyAndShapeAreRejected() {
        assertInvalid(request(adjustments: adjustmentsJSON(["vibrance": "1"])), contains: "unknown adjustments key(s): vibrance")
        assertInvalid(request(adjustments: "[]"), contains: "adjustments must be a JSON object")
        assertInvalid(request(adjustments: "1"), contains: "adjustments must be a JSON object")
    }

    func testRangesAreCheckedWithoutClamping() throws {
        // 境界値は通る。
        let low = adjustmentsJSON([
            "exposure": "-2", "wb_temp": "-100", "shadow_lift": "0", "highlight_recovery": "0", "brightness": "0.7",
            "contrast": "0.8", "saturation": "0", "sharpness": "0.8", "noise_reduction": "\"off\"",
        ])
        XCTAssertNoThrow(try parse(request(adjustments: low)))
        let high = adjustmentsJSON([
            "exposure": "2", "wb_temp": "100", "shadow_lift": "100", "highlight_recovery": "100", "brightness": "1.4",
            "contrast": "1.4", "saturation": "1.5", "sharpness": "1.6", "noise_reduction": "\"light\"",
        ])
        XCTAssertNoThrow(try parse(request(adjustments: high)))
        // 範囲外はクランプせず拒否。
        let outside: [(String, String)] = [
            ("exposure", "2.01"), ("exposure", "-2.5"), ("wb_temp", "101"), ("shadow_lift", "-1"),
            ("highlight_recovery", "100.5"), ("brightness", "0.69"), ("contrast", "1.5"), ("saturation", "-0.1"),
            ("sharpness", "0.79"), ("sharpness", "1.61"),
        ]
        for (key, value) in outside {
            assertInvalid(request(adjustments: adjustmentsJSON([key: value])), contains: "adjustments.\(key) must be a number")
        }
    }

    func testTypesAreStrict() {
        assertInvalid(request(adjustments: adjustmentsJSON(["exposure": "true"])), contains: "adjustments.exposure")
        assertInvalid(request(adjustments: adjustmentsJSON(["contrast": "\"1\""])), contains: "adjustments.contrast")
        assertInvalid(request(adjustments: adjustmentsJSON(["noise_reduction": "\"medium\""])), contains: "noise_reduction")
        assertInvalid(request(adjustments: adjustmentsJSON(["noise_reduction": "1"])), contains: "noise_reduction")
    }

    func testAdjustmentsRequireAppleDefaultProfile() {
        assertInvalid(request(adjustments: adjustmentsJSON(), settings: #""settings":{"profile":"neutral"}"#), contains: "apple_default")
        // settings 省略 = neutral なので不可。
        assertInvalid("{\(Self.base),\"adjustments\":\(adjustmentsJSON())}", contains: "apple_default")
        assertInvalid(request(adjustments: adjustmentsJSON(), settings: #""settings":{"lens_correction":true}"#), contains: "apple_default")
    }

    func testAdjustmentsAreExclusiveWithExposureAndWhiteBalanceSettings() throws {
        // 値に関わらず指定自体が排他（0 でも不可）。null は省略扱いで通る。
        for (key, value) in [("exposure_ev", "0"), ("exposure_ev", "0.5"), ("neutral_temperature", "5200"), ("neutral_tint", "0")] {
            let settings = "\"settings\":{\"profile\":\"apple_default\",\"\(key)\":\(value)}"
            assertInvalid(request(adjustments: adjustmentsJSON(), settings: settings), contains: "cannot be combined with settings.\(key)")
        }
        let nulls = #""settings":{"profile":"apple_default","exposure_ev":null,"neutral_temperature":null,"neutral_tint":null}"#
        XCTAssertNotNil(try parse(request(adjustments: adjustmentsJSON(), settings: nulls)).adjustments)
        // adjustments が無ければ従来どおり指定できる。
        XCTAssertEqual(try parse("{\(Self.base),\"settings\":{\"profile\":\"apple_default\",\"exposure_ev\":0.5}}").settings.exposureEV, 0.5)
    }

    func testIndividualOverridesCanBeCombined() throws {
        let settings = #""settings":{"profile":"apple_default","luminance_noise_reduction":0,"sharpness_amount":0}"#
        let request = try parse(request(adjustments: adjustmentsJSON(), settings: settings))
        XCTAssertNotNil(request.adjustments)
        XCTAssertEqual(request.settings.luminanceNoiseReduction, 0)
        XCTAssertEqual(request.settings.sharpnessAmount, 0)
    }
}
