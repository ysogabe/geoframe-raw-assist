import Foundation
import XCTest

@testable import CoreImageDevelop

/// `--batch`（CLI 0.5.0、Issue #82）の行処理。実 ARW を使うものは無ければ skip（入力は読むだけ）。
final class BatchTests: XCTestCase {
    static let landscape = "/Volumes/Backup/100MSDCF/DSC04430.ARW"
    static let portrait = "/Volumes/Backup/100MSDCF/DSC04553.ARW"

    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("ci-batch-\(UUID().uuidString)")
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

    private func object(_ line: String) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any], line)
    }

    /// 行の並びをバッチで処理し、応答行を返す。
    private func runBatch(_ lines: [String], processor: BatchProcessor = BatchProcessor()) -> [String] {
        var iterator = lines.makeIterator()
        var responses: [String] = []
        processor.run(next: { iterator.next() }, emit: { responses.append($0); return true })
        return responses
    }

    // MARK: - ARW 不要

    func testSecondOfThreeFailingStillYieldsThreeResponses() throws {
        // 1 件目・3 件目は検証で落ちる正しい JSON、2 件目は壊れた JSON。どれも 1 件として番号を振り、応答を返す。
        let responses = runBatch([
            #"{"input":"/nonexistent/a.ARW","output":"/tmp/a.jpg","mode":"preview"}"#,
            #"{"input": broken"#,
            #"{"input":"/nonexistent/c.ARW","output":"/tmp/c.jpg","mode":"preview"}"#,
        ])
        XCTAssertEqual(responses.count, 3)
        let objects = try responses.map(object)
        XCTAssertEqual(objects.map { $0["sequence"] as? Int }, [1, 2, 3])
        XCTAssertEqual(objects.map { $0["ok"] as? Bool }, [false, false, false])
        let codes = objects.map { ($0["error"] as? [String: Any])?["code"] as? String }
        XCTAssertEqual(codes, ["INPUT_NOT_FOUND", "INVALID_REQUEST", "INPUT_NOT_FOUND"])
        XCTAssertTrue(responses.allSatisfy { !$0.contains("\n") })
    }

    func testBlankLinesAreSkippedWithoutConsumingSequence() throws {
        let processor = BatchProcessor()
        XCTAssertNil(processor.handle(line: ""))
        XCTAssertNil(processor.handle(line: "   \t"))
        XCTAssertNil(processor.handle(line: "\r"))
        XCTAssertEqual(processor.sequence, 0)
        let response = try object(XCTUnwrap(processor.handle(line: "[]")))
        XCTAssertEqual(response["sequence"] as? Int, 1)
        XCTAssertEqual((response["error"] as? [String: Any])?["code"] as? String, "INVALID_REQUEST")
        XCTAssertNil(processor.handle(line: ""))
        XCTAssertEqual(try object(XCTUnwrap(processor.handle(line: "nope")))["sequence"] as? Int, 2)
    }

    func testRunStopsWhenEmitFails() {
        let lines = ["x", "y", "z"]
        var iterator = lines.makeIterator()
        var emitted = 0
        let count = BatchProcessor().run(next: { iterator.next() }, emit: { _ in emitted += 1; return emitted < 2 })
        XCTAssertEqual(emitted, 2)
        XCTAssertEqual(count, 2)
    }

    func testSingleShotResponsesHaveNoSequenceKey() {
        let failure = encodeJSONLine(FailureResponse(DevelopError(.invalidRequest, "x")))
        XCTAssertEqual(failure, #"{"error":{"code":"INVALID_REQUEST","message":"x"},"ok":false}"#)
        let batchFailure = encodeJSONLine(FailureResponse(DevelopError(.invalidRequest, "x"), sequence: 7))
        XCTAssertEqual(batchFailure, #"{"error":{"code":"INVALID_REQUEST","message":"x"},"ok":false,"sequence":7}"#)
    }

    // MARK: - 実 ARW（スモーク）

    func testSecondOfThreeFailingWithRealRAW() throws {
        try requireFile(Self.landscape)
        let a = directory.appendingPathComponent("a.jpg")
        let c = directory.appendingPathComponent("c.jpg")
        let responses = runBatch([
            #"{"input":"\#(Self.landscape)","output":"\#(a.path)","mode":"preview","max_edge":300}"#,
            #"{"input":"\#(Self.landscape)","output":"\#(directory.path)/missing-dir/b.jpg","mode":"preview","max_edge":300}"#,
            #"{"input":"\#(Self.landscape)","output":"\#(c.path)","mode":"preview","max_edge":300}"#,
        ])
        XCTAssertEqual(responses.count, 3)
        let objects = try responses.map(object)
        XCTAssertEqual(objects.map { $0["sequence"] as? Int }, [1, 2, 3])
        XCTAssertEqual(objects.map { $0["ok"] as? Bool }, [true, false, true])
        XCTAssertEqual((objects[1]["error"] as? [String: Any])?["code"] as? String, "OUTPUT_DIR_NOT_FOUND")
        XCTAssertEqual(objects[2]["width"] as? Int, 300)
        XCTAssertEqual(try Data(contentsOf: a), try Data(contentsOf: c))
    }

    /// バッチ（CIContext 使い回し・CIRAWFilter は毎回生成）と単発（どちらも毎回生成）で出力がバイト一致し、
    /// 前のリクエストの設定（neutral・個別上書き・adjustments・別ファイル）が次へ漏れないこと。
    func testBatchOutputIsByteIdenticalToSingleShotAndDoesNotLeakSettings() throws {
        try requireFile(Self.landscape)
        try requireFile(Self.portrait)
        func request(_ input: String, _ output: URL, extra: String) -> String {
            #"{"input":"\#(input)","output":"\#(output.path)","mode":"preview","max_edge":320,"format":"tiff16","decoder_version":"9",\#(extra)}"#
        }
        let base = #""settings":{"profile":"apple_default","lens_correction":true}"#
        let neutral = #""settings":{"profile":"neutral","exposure_ev":1.5,"neutral_temperature":3000,"neutral_tint":20}"#
        let overrides = #""settings":{"profile":"apple_default","lens_correction":false,"luminance_noise_reduction":0,"boost_amount":0.5,"boost_shadow_amount":1.4,"gamut_mapping":false,"baseline_exposure":0.1},"diagnostics":{"stats":false,"linear_space_filter":{"kind":"scale","value":0.5}}"#
        let adjustments = #""settings":{"profile":"apple_default","lens_correction":true},"adjustments":{"exposure":0.7,"wb_temp":30,"sharpness":1.4,"noise_reduction":"full","shadow_lift":20,"brightness":1.1,"contrast":1.2,"saturation":1.3,"highlight_recovery":0}"#

        // 同じ input が続く: 基本 → neutral → 上書き+診断 → adjustments → 基本（1 件目と同じ）→ 別ファイル → 基本。
        let plan: [(String, String)] = [
            (Self.landscape, base), (Self.landscape, neutral), (Self.landscape, overrides),
            (Self.landscape, adjustments), (Self.landscape, base), (Self.portrait, base), (Self.landscape, base),
        ]
        let batchOutputs = plan.indices.map { directory.appendingPathComponent("batch-\($0).tif") }
        let responses = runBatch(plan.indices.map { request(plan[$0].0, batchOutputs[$0], extra: plan[$0].1) })
        XCTAssertEqual(responses.count, plan.count)
        for (index, line) in responses.enumerated() {
            let response = try object(line)
            XCTAssertEqual(response["ok"] as? Bool, true, line)
            XCTAssertEqual(response["sequence"] as? Int, index + 1)
        }

        for (index, (input, extra)) in plan.enumerated() {
            let single = directory.appendingPathComponent("single-\(index).tif")
            _ = try develop(RequestParser.parse(Data(request(input, single, extra: extra).utf8)))
            XCTAssertEqual(try Data(contentsOf: batchOutputs[index]), try Data(contentsOf: single), "request #\(index + 1)")
        }
        // 前後に別設定を挟んでも、同じ基本リクエストは同じバイト列になる。
        XCTAssertEqual(try Data(contentsOf: batchOutputs[0]), try Data(contentsOf: batchOutputs[4]))
        XCTAssertEqual(try Data(contentsOf: batchOutputs[0]), try Data(contentsOf: batchOutputs[6]))
    }
}
