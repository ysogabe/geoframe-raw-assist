import Foundation
import ImageIO
import XCTest

@testable import CoreImageDevelop

/// 実 ARW がある環境でだけ走るスモークテスト（無ければ skip）。入力は読むだけ。
final class SmokeTests: XCTestCase {
    static let landscape = "/Volumes/Backup/100MSDCF/DSC04430.ARW"
    static let portrait = "/Volumes/Backup/100MSDCF/DSC04553.ARW"

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
}
