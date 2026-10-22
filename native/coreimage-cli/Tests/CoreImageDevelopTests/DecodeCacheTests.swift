import CoreImage
import Foundation
import ImageIO
import XCTest

@testable import CoreImageDevelop

/// `--batch` の decode キャッシュ op（CLI 0.7.0、Issue #116）。実 ARW を使うものは無ければ skip（入力は読むだけ）。
/// ARW は環境変数 `GEOFRAME_TEST_ARW`、無ければ作業マシンの work/raw の ARW を探す。
final class DecodeCacheTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("ci-decode-cache-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private func sampleARW() throws -> String {
        var candidates: [String] = []
        if let env = ProcessInfo.processInfo.environment["GEOFRAME_TEST_ARW"] { candidates.append(env) }
        let rawDir = "/Users/yoshio/works/60-misc/40-raw-assistant/work/raw"
        if let names = try? FileManager.default.contentsOfDirectory(atPath: rawDir) {
            candidates += names.filter { $0.lowercased().hasSuffix(".arw") }.sorted().map { "\(rawDir)/\($0)" }
        }
        candidates.append("/Volumes/Backup/100MSDCF/DSC04430.ARW")
        guard let path = candidates.first(where: { FileManager.default.isReadableFile(atPath: $0) }) else {
            throw XCTSkip("sample RAW not available (set GEOFRAME_TEST_ARW)")
        }
        return path
    }

    private func object(_ line: String?) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(try XCTUnwrap(line).utf8)) as? [String: Any], line ?? "nil")
    }

    private func json(_ dict: [String: Any]) -> String {
        String(data: try! JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]), encoding: .utf8)!
    }

    private func adjustments(
        exposure: Double = 0, wb: Double = 0, lift: Double = 0, hr: Double = 0, brightness: Double = 1,
        contrast: Double = 1, saturation: Double = 1, sharpness: Double = 1, nr: String = "light"
    ) -> [String: Any] {
        [
            "exposure": exposure, "wb_temp": wb, "shadow_lift": lift, "highlight_recovery": hr, "brightness": brightness,
            "contrast": contrast, "saturation": saturation, "sharpness": sharpness, "noise_reduction": nr,
        ]
    }

    private let settings: [String: Any] = ["profile": "apple_default", "lens_correction": true]

    private func errorCode(_ line: String?) throws -> String? {
        let response = try object(line)
        XCTAssertEqual(response["ok"] as? Bool, false)
        return (response["error"] as? [String: Any])?["code"] as? String
    }

    // MARK: - ARW 不要

    private func fakeEntry(_ id: String, bytes: Int) -> DecodedEntry {
        let adjustments = Adjustments()
        let filterless = DecoderDefaults(sharpnessAmount: 1, luminanceNoiseReduction: 0, colorNoiseReduction: 0, neutralTemperature: 5000, neutralTint: 0)
        let applied = Applied(
            profile: "apple_default", decoder_version: "9", scale_factor: 1, exposure: 0, baseline_exposure: 0, boost_amount: 0,
            boost_shadow_amount: 0, shadow_bias: 0, neutral_temperature: 5000, neutral_tint: 0, sharpness_amount: 1,
            contrast_amount: 0, detail_amount: 0, luminance_noise_reduction_amount: 0, color_noise_reduction_amount: 0,
            moire_reduction_amount: 0, local_tone_map_amount: 0, extended_dynamic_range_amount: 0, gamut_mapping_enabled: true,
            draft_mode_enabled: false, lens_correction_enabled: true, lens_correction_supported: true,
            local_tone_map_supported: false, orientation: 1, working_color_space: "", color_space: "", bit_depth: 16,
            orientation_handling: ""
        )
        return DecodedEntry(
            id: id, input: URL(fileURLWithPath: "/tmp/x.ARW"), mode: .preview, stage: DecodeStageKey(adjustments),
            bitmap: Data(count: bytes), extent: CGRect(x: 0, y: 0, width: 1, height: 1), iso: nil,
            plan: AdjustmentsDecoderPlan(defaults: filterless, miredShift: 0), guards: [], applied: applied,
            source: SourceInfo(camera_model: nil, lens_model: nil, iso: nil, native_width: 1, native_height: 1, orientation: 1),
            profile: .appleDefault
        )
    }

    func testLRUEvictsOldestByCount() {
        let cache = DecodeCache(limits: DecodeCacheLimits(maxEntries: 3, maxBytes: 1_000_000))
        XCTAssertEqual(cache.insert(fakeEntry("a", bytes: 10)), [])
        XCTAssertEqual(cache.insert(fakeEntry("b", bytes: 10)), [])
        XCTAssertEqual(cache.insert(fakeEntry("c", bytes: 10)), [])
        XCTAssertNotNil(cache.touch("a"))  // a を最新に → 古い順は b, c, a
        XCTAssertEqual(cache.insert(fakeEntry("d", bytes: 10)), ["b"])
        XCTAssertEqual(cache.handles, ["c", "a", "d"])
        XCTAssertNil(cache.touch("b"))
        XCTAssertTrue(cache.wasEvicted("b"))
        XCTAssertFalse(cache.wasEvicted("zzz"))
        XCTAssertEqual(cache.bytes, 30)
    }

    func testLRUEvictsByBytesButKeepsNewEntry() {
        let cache = DecodeCache(limits: DecodeCacheLimits(maxEntries: 10, maxBytes: 100))
        _ = cache.insert(fakeEntry("a", bytes: 40))
        _ = cache.insert(fakeEntry("b", bytes: 40))
        XCTAssertEqual(cache.insert(fakeEntry("c", bytes: 40)), ["a"])
        XCTAssertEqual(cache.bytes, 80)
        // 新しい 1 件が単独で上限に近くても、新規は捨てない。古い方を全部捨てる。
        XCTAssertEqual(cache.insert(fakeEntry("d", bytes: 90)), ["b", "c"])
        XCTAssertEqual(cache.handles, ["d"])
        XCTAssertTrue(cache.remove("d"))
        XCTAssertFalse(cache.remove("d"))
        XCTAssertEqual(cache.bytes, 0)
    }

    func testOpValidationAndLegacyCompatibility() throws {
        let processor = BatchProcessor()
        // 未知の op / op が文字列でない
        XCTAssertEqual(try errorCode(processor.handle(line: #"{"op":"nope"}"#)), "INVALID_REQUEST")
        XCTAssertEqual(try errorCode(processor.handle(line: #"{"op":null}"#)), "INVALID_REQUEST")
        XCTAssertEqual(try errorCode(processor.handle(line: #"{"op":3}"#)), "INVALID_REQUEST")
        // op なしは従来どおり（未知キーは INVALID_REQUEST、存在しない入力は INPUT_NOT_FOUND）
        XCTAssertEqual(try errorCode(processor.handle(line: #"{"input":"/nonexistent/a.ARW","output":"/tmp/a.jpg","mode":"preview"}"#)), "INPUT_NOT_FOUND")
        XCTAssertEqual(try errorCode(processor.handle(line: #"{"input":"/nonexistent/a.ARW","output":"/tmp/a.jpg","mode":"preview","bogus":1}"#)), "INVALID_REQUEST")
        // decode: adjustments 必須・未知キー・存在しない入力
        XCTAssertEqual(try errorCode(processor.handle(line: #"{"op":"decode","input":"/nonexistent/a.ARW","mode":"preview"}"#)), "INVALID_REQUEST")
        XCTAssertEqual(try errorCode(processor.handle(line: json(["op": "decode", "input": "/nonexistent/a.ARW", "mode": "preview", "settings": settings, "adjustments": adjustments(), "output": "/tmp/x.tif"]))), "INVALID_REQUEST")
        XCTAssertEqual(try errorCode(processor.handle(line: json(["op": "decode", "input": "/nonexistent/a.ARW", "mode": "preview", "settings": settings, "adjustments": adjustments()]))), "INPUT_NOT_FOUND")
        // render: 未知のハンドル
        let render = json(["op": "render", "handle": "nope", "output": directory.appendingPathComponent("o.tif").path, "format": "tiff16", "adjustments": adjustments()])
        XCTAssertEqual(try errorCode(processor.handle(line: render)), "HANDLE_NOT_FOUND")
        XCTAssertEqual(try errorCode(processor.handle(line: #"{"op":"render","handle":"","output":"/tmp/o.tif","format":"tiff16"}"#)), "INVALID_REQUEST")
        // release は冪等（未知でも ok）
        let release = try object(processor.handle(line: #"{"op":"release","handle":"nope"}"#))
        XCTAssertEqual(release["ok"] as? Bool, true)
        XCTAssertEqual(release["op"] as? String, "release")
        XCTAssertEqual(release["released"] as? Bool, false)
        XCTAssertEqual(release["sequence"] as? Int, 11)
    }

    // MARK: - 実 ARW

    private func pixels(_ url: URL) throws -> (width: Int, height: Int, bytes: [UInt8]) {
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        let data = try XCTUnwrap(image.dataProvider?.data) as Data
        return (image.width, image.height, [UInt8](data))
    }

    /// 16bit（リトルエンディアン）の画素差。バイト列が同一なら (0, 0, 0)。
    private func diff16(_ a: [UInt8], _ b: [UInt8]) -> (maxDiff: Int, differing: Int, mean: Double) {
        XCTAssertEqual(a.count, b.count)
        var maxDiff = 0
        var differing = 0
        var sum = 0
        var i = 0
        while i + 1 < min(a.count, b.count) {
            let x = Int(a[i]) | Int(a[i + 1]) << 8
            let y = Int(b[i]) | Int(b[i + 1]) << 8
            if x != y {
                differing += 1
                sum += abs(x - y)
                maxDiff = max(maxDiff, abs(x - y))
            }
            i += 2
        }
        return (maxDiff, differing, Double(sum) / Double(max(1, a.count / 2)))
    }

    /// 通常の経路（op なし）と decode → render の経路で、同じ入力・設定の画素が一致する。
    func testRenderMatchesDirectDevelopPixelForPixel() throws {
        let arw = try sampleARW()
        let processor = BatchProcessor()
        let base: [String: Any] = ["input": arw, "mode": "preview", "max_edge": 640, "settings": settings]
        // 後段の各キー（lift / brightness / contrast / saturation / USM）を動かす。hr は exposure と lift の両方に効く。
        let cases: [(String, [String: Any])] = [
            ("identity", adjustments()),
            ("post-all", adjustments(lift: 30, brightness: 1.1, contrast: 1.15, saturation: 1.2, sharpness: 1.4)),
            ("hr", adjustments(hr: 60)),
            ("exp+wb+nr", adjustments(exposure: 0.5, wb: 20, lift: 10, brightness: 0.9, contrast: 1.05, sharpness: 0.9, nr: "full")),
        ]
        var report: [String] = []
        for (name, adj) in cases {
            // 通常経路
            let directPath = directory.appendingPathComponent("direct-\(name).tif").path
            var direct = base
            direct["output"] = directPath
            direct["format"] = "tiff16"
            direct["output_color_space"] = "srgb"
            direct["adjustments"] = adj
            let directResponse = try object(processor.handle(line: json(direct)))
            XCTAssertEqual(directResponse["ok"] as? Bool, true, "\(directResponse)")

            // decode → render
            var decode = base
            decode["op"] = "decode"
            decode["adjustments"] = adj
            let decodeResponse = try object(processor.handle(line: json(decode)))
            XCTAssertEqual(decodeResponse["ok"] as? Bool, true, "\(decodeResponse)")
            let handle = try XCTUnwrap(decodeResponse["handle"] as? String)
            let cachedPath = directory.appendingPathComponent("cached-\(name).tif").path
            let renderResponse = try object(processor.handle(line: json([
                "op": "render", "handle": handle, "output": cachedPath, "format": "tiff16", "output_color_space": "srgb", "adjustments": adj,
            ])))
            XCTAssertEqual(renderResponse["ok"] as? Bool, true, "\(renderResponse)")
            XCTAssertEqual(renderResponse["op"] as? String, "render")
            XCTAssertEqual(renderResponse["handle"] as? String, handle)

            let a = try pixels(URL(fileURLWithPath: directPath))
            let b = try pixels(URL(fileURLWithPath: cachedPath))
            XCTAssertEqual(a.width, b.width)
            XCTAssertEqual(a.height, b.height)
            let d = diff16(a.bytes, b.bytes)
            report.append("\(name): \(a.width)x\(a.height) max_diff16=\(d.maxDiff) mean_abs_diff16=\(d.mean) differing_samples=\(d.differing)")
            // 保持は half float（RGBAh）。通常経路は RAW〜後段が 1 つのグラフとして評価され、デコード結果が half に丸まらない
            // ぶんだけ差が出る（README「decode キャッシュ」）。許容は 8bit の半段（= 16bit で 128）。
            XCTAssertLessThanOrEqual(d.maxDiff, 128, "\(name): pixels differ by more than half an 8-bit level")
            // 応答の adjustments_applied / applied も同じ
            XCTAssertEqual(
                (directResponse["adjustments_applied"] as? NSDictionary), (renderResponse["adjustments_applied"] as? NSDictionary), name
            )
            XCTAssertEqual((directResponse["applied"] as? NSDictionary), (renderResponse["applied"] as? NSDictionary), name)
            XCTAssertEqual((directResponse["source"] as? NSDictionary), (renderResponse["source"] as? NSDictionary), name)
            _ = processor.handle(line: json(["op": "release", "handle": handle]))
        }
        print("PIXEL-EQUALITY:\n" + report.joined(separator: "\n"))
        XCTAssertEqual(processor.decodeCache.count, 0)
    }

    /// 同じハンドルに別の後段を何度掛けても、それぞれ通常経路と一致する（保持した画素が render で壊れない）。
    func testSameHandleRenderedRepeatedlyStaysConsistent() throws {
        let arw = try sampleARW()
        let processor = BatchProcessor()
        let decodeAdj = adjustments(exposure: 0.25)
        let decoded = try object(processor.handle(line: json([
            "op": "decode", "input": arw, "mode": "preview", "max_edge": 640, "settings": settings, "adjustments": decodeAdj,
        ])))
        let handle = try XCTUnwrap(decoded["handle"] as? String)
        for (i, contrast) in [1.0, 1.3, 1.0].enumerated() {
            var adj = decodeAdj
            adj["contrast"] = contrast
            let cachedPath = directory.appendingPathComponent("c\(i).tif").path
            let r = try object(processor.handle(line: json(["op": "render", "handle": handle, "output": cachedPath, "format": "tiff16", "output_color_space": "srgb", "adjustments": adj])))
            XCTAssertEqual(r["ok"] as? Bool, true, "\(r)")
            let directPath = directory.appendingPathComponent("d\(i).tif").path
            let d = try object(processor.handle(line: json([
                "input": arw, "mode": "preview", "max_edge": 640, "settings": settings, "adjustments": adj, "output": directPath, "format": "tiff16", "output_color_space": "srgb",
            ])))
            XCTAssertEqual(d["ok"] as? Bool, true)
            let cached8 = try pixels(URL(fileURLWithPath: cachedPath))
            let direct8 = try pixels(URL(fileURLWithPath: directPath))
            XCTAssertEqual(cached8.bytes.count, direct8.bytes.count)
            XCTAssertLessThanOrEqual(diff16(cached8.bytes, direct8.bytes).maxDiff, 128, "case \(i)")
        }
    }

    func testRenderRejectsDifferentDecodeStageValues() throws {
        let arw = try sampleARW()
        let processor = BatchProcessor()
        let decoded = try object(processor.handle(line: json([
            "op": "decode", "input": arw, "mode": "preview", "max_edge": 640, "settings": settings, "adjustments": adjustments(exposure: 0.5),
        ])))
        let handle = try XCTUnwrap(decoded["handle"] as? String)
        let out = directory.appendingPathComponent("o.tif").path
        func render(_ adj: [String: Any]) throws -> String? {
            processor.handle(line: json(["op": "render", "handle": handle, "output": out, "format": "tiff16", "adjustments": adj]))
        }
        // exposure / wb / NR / sharpness<1 はデコード段 → 食い違いは INVALID_REQUEST
        XCTAssertEqual(try errorCode(render(adjustments(exposure: 0.25))), "INVALID_REQUEST")
        XCTAssertEqual(try errorCode(render(adjustments(exposure: 0.5, wb: 10))), "INVALID_REQUEST")
        XCTAssertEqual(try errorCode(render(adjustments(exposure: 0.5, nr: "full"))), "INVALID_REQUEST")
        XCTAssertEqual(try errorCode(render(adjustments(exposure: 0.5, sharpness: 0.9))), "INVALID_REQUEST")
        // hr は exposure に効く（0.5 + delta ≠ 0.5）
        XCTAssertEqual(try errorCode(render(adjustments(exposure: 0.5, hr: 60))), "INVALID_REQUEST")
        // 後段のキーだけなら通る（sharpness > 1 も後段 USM）
        XCTAssertEqual(try object(render(adjustments(exposure: 0.5, lift: 20, brightness: 1.1, contrast: 1.2, saturation: 0.8, sharpness: 1.3)))["ok"] as? Bool, true)
    }

    func testEvictedHandleReturnsHandleNotFoundAndRedecodeRecovers() throws {
        let arw = try sampleARW()
        let processor = BatchProcessor(decodeCacheLimits: DecodeCacheLimits(maxEntries: 2, maxBytes: 64 * 1024 * 1024))
        func decode() throws -> [String: Any] {
            try object(processor.handle(line: json([
                "op": "decode", "input": arw, "mode": "preview", "max_edge": 320, "settings": settings, "adjustments": adjustments(),
            ])))
        }
        let first = try decode()
        let h1 = try XCTUnwrap(first["handle"] as? String)
        XCTAssertEqual((first["cache"] as? [String: Any])?["evicted"] as? [String], [])
        let h2 = try XCTUnwrap(try decode()["handle"] as? String)
        // h1 を触って最新に → 次の decode で h2 が捨てられる
        let out = directory.appendingPathComponent("o.tif").path
        func render(_ h: String) -> String? {
            processor.handle(line: json(["op": "render", "handle": h, "output": out, "format": "tiff16", "adjustments": adjustments()]))
        }
        XCTAssertEqual(try object(render(h1))["ok"] as? Bool, true)
        let third = try decode()
        XCTAssertEqual((third["cache"] as? [String: Any])?["evicted"] as? [String], [h2])
        XCTAssertEqual(try errorCode(render(h2)), "HANDLE_NOT_FOUND")
        let message = ((try object(render(h2))["error"]) as? [String: Any])?["message"] as? String
        XCTAssertTrue(message?.contains("evicted") == true, message ?? "")
        // 再 decode すれば描ける
        let h4 = try XCTUnwrap(try decode()["handle"] as? String)
        XCTAssertEqual(try object(render(h4))["ok"] as? Bool, true)
        // release 後は NOT_FOUND、release の二重呼びは ok
        let released = try object(processor.handle(line: json(["op": "release", "handle": h4])))
        XCTAssertEqual(released["released"] as? Bool, true)
        XCTAssertEqual(try errorCode(render(h4)), "HANDLE_NOT_FOUND")
        XCTAssertEqual(try object(processor.handle(line: json(["op": "release", "handle": h4])))["released"] as? Bool, false)
    }

    func testDecodeLargerThanByteLimitIsRejected() throws {
        let arw = try sampleARW()
        let processor = BatchProcessor(decodeCacheLimits: DecodeCacheLimits(maxEntries: 4, maxBytes: 1024))
        let response = processor.handle(line: json([
            "op": "decode", "input": arw, "mode": "preview", "max_edge": 640, "settings": settings, "adjustments": adjustments(),
        ]))
        XCTAssertEqual(try errorCode(response), "INVALID_REQUEST")
        XCTAssertEqual(processor.decodeCache.count, 0)
    }

    /// clearCaches()（1 件ごとに呼ばれる）はデコード結果の保持に影響しない。
    func testClearCachesDoesNotDropHeldDecodes() throws {
        let arw = try sampleARW()
        let processor = BatchProcessor()
        let decoded = try object(processor.handle(line: json([
            "op": "decode", "input": arw, "mode": "preview", "max_edge": 320, "settings": settings, "adjustments": adjustments(),
        ])))
        let handle = try XCTUnwrap(decoded["handle"] as? String)
        processor.context.clearCaches()
        XCTAssertEqual(processor.decodeCache.count, 1)
        let r = try object(processor.handle(line: json(["op": "render", "handle": handle, "output": directory.appendingPathComponent("o.tif").path, "format": "tiff16", "adjustments": adjustments()])))
        XCTAssertEqual(r["ok"] as? Bool, true)
    }
}
