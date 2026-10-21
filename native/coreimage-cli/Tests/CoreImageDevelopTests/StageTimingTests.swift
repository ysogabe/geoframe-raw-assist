import Foundation
import XCTest

@testable import CoreImageDevelop

/// 計測フック（`GEOFRAME_CI_TIMING=1`、Issue #102）の stderr 1 行の書式。ARW 不要。
/// フックは時刻を取って stderr に書くだけで、応答 JSON と出力画像には触れない
/// （応答・出力のバイト一致は BatchTests / SmokeTests が既存のまま確かめる）。
final class StageTimingTests: XCTestCase {
    private func at(_ ms: UInt64) -> DispatchTime {
        DispatchTime(uptimeNanoseconds: 1_000_000_000 + ms * 1_000_000)
    }

    func testEnvironmentKeyIsDocumentedName() {
        XCTAssertEqual(StageTiming.environmentKey, "GEOFRAME_CI_TIMING")
    }

    func testLineSplitsStagesInMilliseconds() {
        let line = StageTiming.line(
            t0: at(0), created: at(80), configured: at(95), graphBuilt: at(96),
            renderStart: at(97), rendered: at(497), written: at(502), end: at(503)
        )
        XCTAssertEqual(line, "timing create=80.0 configure=15.0 graph=1.0 render=400.0 encode_write=5.0 other=2.0 total=503.0")
    }

    func testSkipWriteReportsMinusOneForRenderAndEncode() {
        let line = StageTiming.line(
            t0: at(0), created: at(80), configured: at(95), graphBuilt: at(96),
            renderStart: nil, rendered: nil, written: nil, end: at(100)
        )
        XCTAssertEqual(line, "timing create=80.0 configure=15.0 graph=1.0 render=-1.0 encode_write=-1.0 other=4.0 total=100.0")
    }
}
