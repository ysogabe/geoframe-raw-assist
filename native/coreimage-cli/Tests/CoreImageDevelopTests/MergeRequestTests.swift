import XCTest
@testable import CoreImageDevelop

final class MergeRequestTests: XCTestCase {
    private let adjustments = #""exposure":0,"wb_temp":0,"shadow_lift":0,"highlight_recovery":0,"brightness":1,"contrast":1,"saturation":1,"sharpness":1,"noise_reduction":"light""#
    private func parse(_ inputs: String, extra: String = "") throws -> MergeRequest {
        try RequestParser.parseMerge(Data("{\"mode\":\"merge\",\"inputs\":\(inputs),\"output\":\"/tmp/out.jpg\",\"settings\":{\"profile\":\"apple_default\",\"lens_correction\":true},\"adjustments\":{\(adjustments)}\(extra)}".utf8))
    }
    func testAcceptsTwoNineAndPreview() throws {
        XCTAssertEqual(try parse("[\"/tmp/a.ARW\",\"/tmp/b.arw\"]").inputs.count, 2)
        let nine = (0..<9).map { "\"/tmp/\($0).ARW\"" }.joined(separator: ",")
        XCTAssertEqual(try parse("[\(nine)]", extra: ",\"max_edge\":2400").maxEdge, 2400)
    }
    func testRejectsFrameCountDuplicateUnknownAndWrongTypes() {
        XCTAssertThrowsError(try parse("[\"/tmp/a.ARW\"]"))
        XCTAssertThrowsError(try parse("[\"/tmp/a.ARW\",\"/tmp/b.ARW\",\"/tmp/c.ARW\",\"/tmp/d.ARW\",\"/tmp/e.ARW\",\"/tmp/f.ARW\",\"/tmp/g.ARW\",\"/tmp/h.ARW\",\"/tmp/i.ARW\",\"/tmp/j.ARW\"]"))
        XCTAssertThrowsError(try parse("[\"/tmp/a.ARW\",\"/tmp/a.ARW\"]"))
        XCTAssertThrowsError(try parse("[\"/tmp/a.ARW\",\"/tmp/b.ARW\"]", extra: ",\"wat\":1"))
        XCTAssertThrowsError(try parse("[\"/tmp/a.ARW\",\"/tmp/b.ARW\"]", extra: ",\"merge\":{\"align\":1}"))
        XCTAssertThrowsError(try parse("[\"/tmp/a.ARW\",\"/tmp/b.ARW\"]", extra: ",\"merge\":{\"weights\":{\"contrast\":\"nan\"}}"))
    }
    func testOutputOptionsAndMergePolicy() throws {
        XCTAssertThrowsError(try parse("[\"/tmp/a.ARW\",\"/tmp/b.ARW\"]", extra: ",\"format\":\"tiff16\""))
        XCTAssertThrowsError(try parse("[\"/tmp/a.ARW\",\"/tmp/b.ARW\"]", extra: ",\"diagnostics\":{}"))
        let req = try parse("[\"/tmp/a.ARW\",\"/tmp/b.ARW\"]", extra: ",\"merge\":{\"align\":false,\"edge\":\"keep\",\"weights\":{\"exposure\":3}}")
        XCTAssertFalse(req.options.align)
        XCTAssertEqual(req.options.edge, "keep")
        XCTAssertEqual(req.options.weights.exposure, 3)
    }
    func testWorkingMemoryBudgetAcceptsNine24MPButRejectsOversizeAndOverflow() {
        XCTAssertEqual(MergeMemoryBudget.estimate(width: 6000, height: 4000, frames: 9, maxEdge: nil), 3_672_000_000)
        XCTAssertLessThan(3_672_000_000, MergeMemoryBudget.safeBytes(physicalMemory: 16_000_000_000))
        XCTAssertGreaterThan(MergeMemoryBudget.estimate(width: 12000, height: 8000, frames: 9, maxEdge: nil)!, MergeMemoryBudget.safeBytes(physicalMemory: 16_000_000_000))
        XCTAssertNil(MergeMemoryBudget.estimate(width: .max, height: .max, frames: 9, maxEdge: nil))
        XCTAssertEqual(MergeMemoryBudget.safeBytes(physicalMemory: 1_000_000_000), 0)
    }
}
