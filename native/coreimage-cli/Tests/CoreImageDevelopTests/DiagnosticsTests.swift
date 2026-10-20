import CoreImage
import Foundation
import XCTest

@testable import CoreImageDevelop

/// 診断モード（CLI 0.4.0、Issue #92 設計書 §4）の ARW 不要な単体テスト。
final class DiagnosticsTests: XCTestCase {
    static let base = #""input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview""#

    private func parse(_ json: String) throws -> DevelopRequest {
        try RequestParser.parse(Data(json.utf8))
    }

    private func parseDiagnostics(_ diagnostics: String) throws -> Diagnostics? {
        try parse("{\(Self.base),\"diagnostics\":\(diagnostics)}").diagnostics
    }

    private func assertInvalid(_ diagnostics: String, contains fragment: String, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertThrowsError(try parseDiagnostics(diagnostics), file: file, line: line) { error in
            guard let error = error as? DevelopError else {
                return XCTFail("unexpected error type: \(error)", file: file, line: line)
            }
            XCTAssertEqual(error.code, .invalidRequest, file: file, line: line)
            XCTAssertTrue(error.message.contains(fragment), "message: \(error.message)", file: file, line: line)
        }
    }

    // MARK: 解析

    func testAbsentOrNullDiagnosticsIsNormalMode() throws {
        XCTAssertNil(try parse("{\(Self.base)}").diagnostics)
        XCTAssertNil(try parseDiagnostics("null"))
    }

    func testEmptyDiagnosticsMeansStatsOnly() throws {
        let diagnostics = try XCTUnwrap(try parseDiagnostics("{}"))
        XCTAssertEqual(diagnostics, Diagnostics())
        XCTAssertTrue(diagnostics.stats)
        XCTAssertEqual(diagnostics.statsColorSpace, .linearSRGB)
        XCTAssertNil(diagnostics.dumpFloat)
        XCTAssertNil(diagnostics.linearSpaceFilter)
        XCTAssertNil(diagnostics.postFilters)
        XCTAssertFalse(diagnostics.skipWrite)
    }

    func testFullDiagnosticsIsParsed() throws {
        let diagnostics = try XCTUnwrap(try parseDiagnostics("""
        {"stats":false,"stats_color_space":"linear_p3","dump_float":{"path":"/w/d/x_s1.F32","color_space":"linear_p3"},
         "linear_space_filter":{"kind":"scale","value":0.5},
         "post_filters":[{"name":"CIHighlightShadowAdjust","inputHighlightAmount":0.6,"inputShadowAmount":0.0,"inputRadius":0.0},
                         {"name":"CIToneCurve","inputPoint0":[0,0],"inputPoint1":[0.25,0.3],"inputPoint2":[0.5,0.6],"inputPoint3":[0.8,0.9],"inputPoint4":null}],
         "skip_write":true}
        """))
        XCTAssertFalse(diagnostics.stats)
        XCTAssertEqual(diagnostics.statsColorSpace, .linearP3)
        XCTAssertEqual(diagnostics.dumpFloat, DumpFloatRequest(path: URL(fileURLWithPath: "/w/d/x_s1.F32"), colorSpace: .linearP3))
        XCTAssertEqual(diagnostics.linearSpaceFilter, LinearSpaceFilterRequest(kind: .scale, value: 0.5))
        XCTAssertTrue(diagnostics.skipWrite)
        let filters = try XCTUnwrap(diagnostics.postFilters)
        XCTAssertEqual(filters.map(\.name), ["CIHighlightShadowAdjust", "CIToneCurve"])
        XCTAssertEqual(filters[0].parameters["inputHighlightAmount"], .number(0.6))
        XCTAssertEqual(filters[1].parameters["inputPoint3"], .vector([0.8, 0.9]))
        // null は既定のまま（パラメータに載せない）。
        XCTAssertNil(filters[1].parameters["inputPoint4"])
    }

    func testDiagnosticsRejectsUnknownKeysAndWrongTypes() {
        assertInvalid("[]", contains: "diagnostics must be a JSON object")
        assertInvalid(#"{"roi":[0,0,10,10]}"#, contains: "unknown diagnostics key(s): roi")
        assertInvalid(#"{"stats":1}"#, contains: "diagnostics.stats must be a boolean")
        assertInvalid(#"{"skip_write":"yes"}"#, contains: "diagnostics.skip_write must be a boolean")
        assertInvalid(#"{"stats_color_space":"srgb"}"#, contains: "diagnostics.stats_color_space")
    }

    func testDumpFloatPathValidation() throws {
        XCTAssertEqual(try parseDiagnostics(#"{"dump_float":{"path":"/w/x.f32"}}"#)?.dumpFloat?.colorSpace, .linearSRGB)
        assertInvalid(#"{"dump_float":{"path":"w/x.f32"}}"#, contains: "absolute path")
        assertInvalid(#"{"dump_float":{"path":""}}"#, contains: "non-empty string")
        assertInvalid(#"{"dump_float":{}}"#, contains: "non-empty string")
        assertInvalid(#"{"dump_float":{"path":"/w/x.tif"}}"#, contains: ".f32")
        assertInvalid(#"{"dump_float":{"path":"/w/x.f32","dtype":"f16"}}"#, contains: "unknown diagnostics.dump_float key(s): dtype")
        assertInvalid(#"{"dump_float":{"path":"/w/x.f32","color_space":"srgb"}}"#, contains: "dump_float.color_space")
        assertInvalid(#"{"dump_float":"/w/x.f32"}"#, contains: "dump_float must be a JSON object")
    }

    func testDumpPathCannotCollideWithInputOrOutputByName() {
        // .f32 必須なので、input（.ARW）・output（.jpg/.tif）と同じパスは名前の段階で拒否される。
        let sameAsOutput = #"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","diagnostics":{"dump_float":{"path":"/b/./o.jpg"}}}"#
        XCTAssertThrowsError(try parse(sameAsOutput))
        let sameAsInput = #"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","diagnostics":{"dump_float":{"path":"/a/x.ARW"}}}"#
        XCTAssertThrowsError(try parse(sameAsInput))
        XCTAssertNoThrow(try parse(#"{"input":"/a/x.ARW","output":"/b/o.jpg","mode":"preview","diagnostics":{"dump_float":{"path":"/b/o.f32"}}}"#))
    }

    func testDumpPathFileSystemChecks() throws {
        let fm = FileManager.default
        let dir = fm.temporaryDirectory.appendingPathComponent("ci-diag-\(UUID().uuidString)")
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: dir) }
        let input = dir.appendingPathComponent("x.ARW")
        try Data([0]).write(to: input)
        let output = dir.appendingPathComponent("o.jpg")

        // 親ディレクトリが無い → OUTPUT_DIR_NOT_FOUND。
        XCTAssertThrowsError(try PathValidator.validateDump(
            dir.appendingPathComponent("missing/d.f32"), input: input, output: output, fileManager: fm
        )) { XCTAssertEqual(($0 as? DevelopError)?.code, .outputDirNotFound) }
        // 既存ディレクトリ → INVALID_REQUEST。
        let asDirectory = dir.appendingPathComponent("dir.f32")
        try fm.createDirectory(at: asDirectory, withIntermediateDirectories: false)
        XCTAssertThrowsError(try PathValidator.validateDump(asDirectory, input: input, output: output, fileManager: fm)) {
            XCTAssertEqual(($0 as? DevelopError)?.code, .invalidRequest)
        }
        // input を指すシンボリックリンク → INVALID_REQUEST（原本へ書かない）。
        let link = dir.appendingPathComponent("link.f32")
        try fm.createSymbolicLink(at: link, withDestinationURL: input)
        XCTAssertThrowsError(try PathValidator.validateDump(link, input: input, output: output, fileManager: fm)) {
            XCTAssertEqual(($0 as? DevelopError)?.code, .invalidRequest)
        }
        // 通常の新規パスは通る。
        XCTAssertNoThrow(try PathValidator.validateDump(dir.appendingPathComponent("ok.f32"), input: input, output: output, fileManager: fm))
    }

    func testLinearSpaceFilterValidation() throws {
        XCTAssertEqual(try parseDiagnostics(#"{"linear_space_filter":{"kind":"identity"}}"#)?.linearSpaceFilter,
                       LinearSpaceFilterRequest(kind: .identity, value: nil))
        XCTAssertEqual(try parseDiagnostics(#"{"linear_space_filter":{"kind":"identity","value":null}}"#)?.linearSpaceFilter?.kind, .identity)
        XCTAssertEqual(try parseDiagnostics(#"{"linear_space_filter":{"kind":"constant","value":0}}"#)?.linearSpaceFilter?.value, 0)
        XCTAssertEqual(try parseDiagnostics(#"{"linear_space_filter":{"kind":"constant","value":8}}"#)?.linearSpaceFilter?.value, 8)
        XCTAssertEqual(try parseDiagnostics(#"{"linear_space_filter":{"kind":"scale","value":0.01}}"#)?.linearSpaceFilter?.value, 0.01)
        assertInvalid(#"{"linear_space_filter":{"kind":"identity","value":1}}"#, contains: "not allowed with kind \"identity\"")
        assertInvalid(#"{"linear_space_filter":{"kind":"scale"}}"#, contains: "linear_space_filter.value")
        assertInvalid(#"{"linear_space_filter":{"kind":"scale","value":0}}"#, contains: "0.01...8.0")
        assertInvalid(#"{"linear_space_filter":{"kind":"scale","value":8.5}}"#, contains: "linear_space_filter.value")
        assertInvalid(#"{"linear_space_filter":{"kind":"constant","value":-0.1}}"#, contains: "0.0...8.0")
        assertInvalid(#"{"linear_space_filter":{"kind":"constant","value":true}}"#, contains: "linear_space_filter.value")
        assertInvalid(#"{"linear_space_filter":{"kind":"gamma","value":1}}"#, contains: "linear_space_filter.kind")
        assertInvalid(#"{"linear_space_filter":{"kind":"scale","value":1,"bias":0}}"#, contains: "unknown diagnostics.linear_space_filter key(s): bias")
    }

    func testLinearSpaceFilterMatrix() {
        XCTAssertTrue(LinearSpaceFilterRequest(kind: .identity, value: nil).matrix == (1, 0))
        XCTAssertTrue(LinearSpaceFilterRequest(kind: .scale, value: 0.5).matrix == (0.5, 0))
        XCTAssertTrue(LinearSpaceFilterRequest(kind: .constant, value: 1.2).matrix == (0, 1.2))
        let filter = LinearSpaceFilterRequest(kind: .constant, value: 0.5).makeFilter()
        XCTAssertEqual(filter.name, "CIColorMatrix")
        XCTAssertEqual((filter.value(forKey: "inputRVector") as? CIVector)?.x, 0)
        XCTAssertEqual((filter.value(forKey: "inputBiasVector") as? CIVector)?.z, 0.5)
        XCTAssertEqual((filter.value(forKey: "inputAVector") as? CIVector)?.w, 1)
    }

    // MARK: post_filters の許可リスト

    func testPostFiltersAllowList() throws {
        XCTAssertEqual(PostFilterAllowList.names, ["CIHighlightShadowAdjust", "CIToneCurve", "CIExposureAdjust", "CIColorClamp"])
        let ok = try XCTUnwrap(try parseDiagnostics("""
        {"post_filters":[{"name":"CIExposureAdjust","inputEV":-1.43},
                         {"name":"CIColorClamp","inputMinComponents":[0,0,0,0],"inputMaxComponents":[1,1,1,1]},
                         {"name":"CIHighlightShadowAdjust"}]}
        """)?.postFilters)
        XCTAssertEqual(ok.count, 3)
        XCTAssertEqual(ok[0].parameters["inputEV"], .number(-1.43))
        XCTAssertEqual(ok[1].parameters["inputMaxComponents"], .vector([1, 1, 1, 1]))
        XCTAssertEqual(ok[2].parameters, [:])
        XCTAssertEqual(try parseDiagnostics(#"{"post_filters":[]}"#)?.postFilters, [])

        assertInvalid(#"{"post_filters":{"name":"CIExposureAdjust"}}"#, contains: "post_filters must be an array")
        assertInvalid(#"{"post_filters":[{"name":"CIGaussianBlur","inputRadius":2}]}"#, contains: "post_filters[0].name must be one of")
        assertInvalid(#"{"post_filters":[{"inputEV":1}]}"#, contains: "post_filters[0].name must be a string")
        assertInvalid(#"{"post_filters":["CIExposureAdjust"]}"#, contains: "post_filters[0] must be a JSON object")
        assertInvalid(#"{"post_filters":[{"name":"CIExposureAdjust","inputImage":1}]}"#, contains: "unknown diagnostics.post_filters[0] (CIExposureAdjust) parameter key(s): inputImage")
        assertInvalid(#"{"post_filters":[{"name":"CIExposureAdjust","inputEV":6}]}"#, contains: "post_filters[0].inputEV must be a number in -5.0...5.0")
        assertInvalid(#"{"post_filters":[{"name":"CIExposureAdjust","inputEV":"1"}]}"#, contains: "inputEV")
        assertInvalid(#"{"post_filters":[{"name":"CIExposureAdjust","inputEV":true}]}"#, contains: "inputEV")
        assertInvalid(#"{"post_filters":[{"name":"CIHighlightShadowAdjust","inputHighlightAmount":1.2}]}"#, contains: "inputHighlightAmount")
        assertInvalid(#"{"post_filters":[{"name":"CIHighlightShadowAdjust","inputShadowAmount":-1.5}]}"#, contains: "inputShadowAmount")
        assertInvalid(#"{"post_filters":[{"name":"CIHighlightShadowAdjust","inputRadius":11}]}"#, contains: "inputRadius")
        assertInvalid(#"{"post_filters":[{"name":"CIColorClamp","inputMaxComponents":[1,1,1]}]}"#, contains: "[r, g, b, a]")
        assertInvalid(#"{"post_filters":[{"name":"CIExposureAdjust"},{"name":"CIColorClamp","inputMinComponents":[0,0,"0",0]}]}"#, contains: "post_filters[1].inputMinComponents")
        let many = Array(repeating: #"{"name":"CIExposureAdjust"}"#, count: PostFilterAllowList.maxFilters + 1).joined(separator: ",")
        assertInvalid("{\"post_filters\":[\(many)]}", contains: "at most")
    }

    func testToneCurvePointRangesAndMonotonicity() throws {
        // x は 0..1（1.0 超の入力は CIToneCurve が clamp するため。設計書 §9）、y は 0..2。
        XCTAssertNoThrow(try parseDiagnostics(#"{"post_filters":[{"name":"CIToneCurve","inputPoint3":[0.8,1.5],"inputPoint4":[1,2]}]}"#))
        assertInvalid(#"{"post_filters":[{"name":"CIToneCurve","inputPoint4":[2.7,1.0]}]}"#, contains: "inputPoint4 must be [x, y]")
        assertInvalid(#"{"post_filters":[{"name":"CIToneCurve","inputPoint1":[0.2,2.5]}]}"#, contains: "inputPoint1 must be [x, y]")
        assertInvalid(#"{"post_filters":[{"name":"CIToneCurve","inputPoint1":[0.2]}]}"#, contains: "inputPoint1")
        assertInvalid(#"{"post_filters":[{"name":"CIToneCurve","inputPoint5":[1,1]}]}"#, contains: "inputPoint5")
        // 既定の点（0.25, 0.5, 0.75, 1）と合わせて狭義単調増加であること。
        assertInvalid(#"{"post_filters":[{"name":"CIToneCurve","inputPoint1":[0.6,0.6]}]}"#, contains: "strictly increasing")
        assertInvalid(#"{"post_filters":[{"name":"CIToneCurve","inputPoint3":[1,0.9]}]}"#, contains: "strictly increasing")
    }

    func testPostFiltersApplyAndReportParameters() throws {
        let linear = CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!
        let image = CIImage(color: CIColor(red: 0.5, green: 0.25, blue: 2.0, alpha: 1, colorSpace: linear)!).cropped(to: CGRect(x: 0, y: 0, width: 4, height: 2))
        let filters = [
            PostFilterRequest(name: "CIExposureAdjust", parameters: ["inputEV": .number(-1)]),
            PostFilterRequest(name: "CIHighlightShadowAdjust", parameters: ["inputRadius": .number(3)]),
        ]
        let (out, reports) = try DiagnosticsPipeline.applyPostFilters(image, filters)
        // extent は入力のまま（inputRadius で広がらない）。
        XCTAssertEqual(out.extent, image.extent)
        XCTAssertEqual(reports.map(\.name), ["CIExposureAdjust", "CIHighlightShadowAdjust"])
        XCTAssertEqual(reports[0].applied_parameters["inputEV"], .number(-1))
        XCTAssertEqual(reports[1].applied_parameters["inputRadius"], .number(3))
        // 指定しなかったパラメータは CIFilter の既定値を読み戻す。
        XCTAssertNotNil(reports[1].applied_parameters["inputShadowAmount"])
        let json = encodeJSONLine(reports[0])
        XCTAssertEqual(json, #"{"applied_parameters":{"inputEV":-1},"name":"CIExposureAdjust"}"#)

        // CIExposureAdjust -1 は線形値を半分にする（extended 範囲の 2.0 も clamp されない）。
        let context = CIContext(options: [
            .workingColorSpace: CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!, .workingFormat: CIFormat.RGBAh, .cacheIntermediates: false,
        ])
        let (exposed, _) = try DiagnosticsPipeline.applyPostFilters(image, [filters[0]])
        let pixels = try DiagnosticsPipeline.renderFloatRGBA(
            exposed, extent: exposed.extent, colorSpace: DiagnosticsColorSpace.linearSRGB.cgColorSpace, context: context
        )
        XCTAssertEqual(pixels[0], 0.25, accuracy: 1e-3)
        XCTAssertEqual(pixels[1], 0.125, accuracy: 1e-3)
        XCTAssertEqual(pixels[2], 1.0, accuracy: 1e-3)
    }

    // MARK: 統計（合成バッファ）

    private func stats(_ pixels: [[Float]], width: Int, height: Int) -> ImageStats {
        let rgba: [Float] = pixels.flatMap { $0 + [Float(1)] }
        return rgba.withUnsafeBufferPointer { DiagnosticsStats.compute(rgba: $0, width: width, height: height) }
    }

    func testFractionsOnSyntheticBuffer() {
        let ge = DiagnosticsStats.whiteThreshold16
        let s = stats([
            [0.5, 0.5, 0.5],
            [1.0, 1.0, 1.0],          // ちょうど 1.0: gt_1 ではないが ge_1
            [1.5, 1.2, 2.7],          // 3ch とも 1.0 超
            [0.2, 1.01, -0.01],       // G だけ 1.0 超、B は負
            [ge, 0.99, 0.0],          // R は 16bit で白（ge_1）、G は白でない
        ], width: 5, height: 1)
        XCTAssertEqual(s.width, 5)
        XCTAssertEqual(s.height, 1)
        XCTAssertEqual(s.r.frac_gt_1, 0.2, accuracy: 1e-12)
        XCTAssertEqual(s.g.frac_gt_1, 0.4, accuracy: 1e-12)
        XCTAssertEqual(s.b.frac_gt_1, 0.2, accuracy: 1e-12)
        XCTAssertEqual(s.r.frac_ge_1, 0.6, accuracy: 1e-12)
        XCTAssertEqual(s.g.frac_ge_1, 0.6, accuracy: 1e-12)
        XCTAssertEqual(s.b.frac_ge_1, 0.4, accuracy: 1e-12)
        XCTAssertEqual(s.b.frac_lt_0, 0.2, accuracy: 1e-12)
        XCTAssertEqual(s.r.frac_lt_0, 0)
        XCTAssertEqual(s.any_gt_1, 0.4, accuracy: 1e-12)
        XCTAssertEqual(s.all_gt_1, 0.2, accuracy: 1e-12)
        XCTAssertEqual(s.b.max, 2.7, accuracy: 1e-6)
        XCTAssertEqual(s.b.min, -0.01, accuracy: 1e-6)
        XCTAssertEqual(s.r.mean, Double((0.5 + 1.0 + 1.5 + 0.2 + ge) / 5), accuracy: 1e-6)
    }

    func testPercentilesAreExactNearestRank() {
        // 0.001 刻みの 10007 値（素数個・逆順）。順位 min(n−1, Int(n·q)) の値と完全一致すること。
        let n = 10007
        let values = (0..<n).reversed().map { Float($0) * 0.001 - 0.5 }
        let pixels: [[Float]] = values.map { [$0, $0 * 3, 1.0] }
        let s = stats(pixels, width: n, height: 1)
        let sorted = values.sorted()
        for (q, got) in [(0.5, s.r.p50), (0.99, s.r.p99), (0.999, s.r.p999)] {
            XCTAssertEqual(got, readable(sorted[DiagnosticsStats.rank(q, count: n)]), "q=\(q)")
        }
        XCTAssertEqual(s.g.p999, readable(sorted.map { $0 * 3 }[DiagnosticsStats.rank(0.999, count: n)]))
        XCTAssertEqual(DiagnosticsStats.rank(0.5, count: n), n / 2)
        // 定数チャンネル（hi == lo）。
        XCTAssertEqual(s.b.p50, 1)
        XCTAssertEqual(s.b.p999, 1)
        XCTAssertEqual(s.b.frac_gt_1, 0)
        XCTAssertEqual(s.b.frac_ge_1, 1)
    }

    func testPercentilesWithHeavyTiesAndOutliers() {
        // 1.0 に大量に張り付き、外れ値が 1 個（ビンが粗くなる分布でも厳密値を返す）。
        var values = [Float](repeating: 1.0, count: 999)
        values += [1000]
        values += (0..<1000).map { Float($0) / 1000 * 0.3 }
        let s = stats(values.map { [$0, Float(0), Float(0)] }, width: values.count, height: 1)
        let sorted = values.sorted()
        XCTAssertEqual(s.r.p50, readable(sorted[DiagnosticsStats.rank(0.5, count: values.count)]))
        XCTAssertEqual(s.r.p99, 1.0)
        XCTAssertEqual(s.r.p999, 1.0)
        XCTAssertEqual(s.r.max, 1000)
    }

    func testNonFiniteValuesAreExcludedFromMomentsButNotFromDenominator() {
        let s = stats([[.nan, 0, 0], [0.5, 0, 0], [.infinity, 0, 0], [1.5, 0, 0]], width: 2, height: 2)
        XCTAssertEqual(s.r.mean, 1.0, accuracy: 1e-12)
        XCTAssertEqual(s.r.max, 1.5)
        XCTAssertEqual(s.r.min, 0.5)
        XCTAssertEqual(s.r.frac_gt_1, 0.25, accuracy: 1e-12)
        XCTAssertTrue(encodeJSONLine(s).contains("\"width\":2"))
    }

    func testPackRGBDropsAlphaLittleEndian() {
        let rgba: [Float] = [0.5, 1.5, -2, 9, 3, 4, 5, 9]
        let data = DiagnosticsPipeline.packRGB(rgba, pixelCount: 2)
        XCTAssertEqual(data.count, 2 * 3 * 4)
        let floats = data.withUnsafeBytes { raw in
            (0..<6).map { Float(bitPattern: UInt32(littleEndian: raw.loadUnaligned(fromByteOffset: $0 * 4, as: UInt32.self))) }
        }
        XCTAssertEqual(floats, [0.5, 1.5, -2, 3, 4, 5])
    }

    // MARK: 応答の形（設計書 §4.4）

    func testReportEncodingShape() throws {
        let channel = ChannelStats(mean: 0.4, p50: 0.05, p99: 1, p999: 1, max: 1, min: -0.0005, frac_gt_1: 0.0025, frac_ge_1: 0.031, frac_lt_0: 0.0006)
        let report = DiagnosticsReport(
            written: false, stats_color_space: "linear_srgb",
            stats: ImageStats(width: 2400, height: 1600, r: channel, g: channel, b: channel, any_gt_1: 0.38, all_gt_1: 0.25),
            dump_float: DumpFloatReport(path: "/w/x.f32", width: 2400, height: 1600, color_space: "linear_srgb", bytes: 46_080_000),
            linear_space_filter: LinearSpaceFilterReport(kind: "identity", value: nil),
            post_filters: [PostFilterReport(name: "CIExposureAdjust", applied_parameters: ["inputEV": .number(-1)])],
            readback: DiagnosticsReadback(gamut_mapping_enabled: true, baseline_exposure: 0.4, extent: [0, 0, 2400, 1600])
        )
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(encodeJSONLine(report).utf8)) as? [String: Any])
        XCTAssertEqual(Set(object.keys), [
            "cli_diagnostics_version", "written", "stats_color_space", "stats_source", "stats", "dump_float",
            "linear_space_filter", "post_filters", "readback",
        ])
        XCTAssertEqual(object["cli_diagnostics_version"] as? Int, 1)
        XCTAssertEqual(object["stats_source"] as? String, "render_input")
        let stats = try XCTUnwrap(object["stats"] as? [String: Any])
        XCTAssertEqual(Set(stats.keys), ["width", "height", "r", "g", "b", "any_gt_1", "all_gt_1"])
        XCTAssertEqual(Set(try XCTUnwrap(stats["r"] as? [String: Any]).keys),
                       ["mean", "p50", "p99", "p999", "max", "min", "frac_gt_1", "frac_ge_1", "frac_lt_0"])
        let dump = try XCTUnwrap(object["dump_float"] as? [String: Any])
        XCTAssertEqual(Set(dump.keys), ["path", "width", "height", "channels", "dtype", "byte_order", "layout", "color_space", "bytes"])
        XCTAssertEqual(dump["layout"] as? String, "interleaved_rgb_row_major")
        XCTAssertEqual(dump["dtype"] as? String, "float32")
        XCTAssertEqual(dump["byte_order"] as? String, "little")
        XCTAssertEqual(dump["channels"] as? Int, 3)
        let linear = try XCTUnwrap(object["linear_space_filter"] as? [String: Any])
        XCTAssertTrue(linear["value"] is NSNull)
        XCTAssertEqual(linear["filter"] as? String, "CIColorMatrix")
        let readback = try XCTUnwrap(object["readback"] as? [String: Any])
        XCTAssertEqual(readback["extent"] as? [Double], [0, 0, 2400, 1600])

        // stats: false・dump 無し等は省略（キーごと出さない）。
        let minimal = DiagnosticsReport(
            written: true, stats_color_space: "linear_p3", stats: nil, dump_float: nil, linear_space_filter: nil, post_filters: nil,
            readback: DiagnosticsReadback(gamut_mapping_enabled: false, baseline_exposure: 0, extent: [0, 0, 1, 1])
        )
        let minimalObject = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(encodeJSONLine(minimal).utf8)) as? [String: Any])
        XCTAssertEqual(Set(minimalObject.keys), ["cli_diagnostics_version", "written", "stats_color_space", "stats_source", "readback"])
    }

    func testCapabilitiesAdvertiseDiagnostics() {
        let json = encodeJSONLine(Capabilities(probe: nil))
        XCTAssertTrue(json.contains(#""diagnostics":{"post_filters":["CIHighlightShadowAdjust","CIToneCurve","CIExposureAdjust","CIColorClamp"],"supported":true}"#), json)
    }
}
