import CoreGraphics
import Foundation
import ImageIO
import XCTest

@testable import CoreImageDevelop

final class MergeCoreTests: XCTestCase {
    func testPyramidMatchesOpenCVOddSizeOracle() throws {
        let data = try Data(contentsOf: XCTUnwrap(Bundle.module.url(forResource: "merge-pyramid-oracle", withExtension: "json")))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let up = try XCTUnwrap(root["up"] as? [String: Any])
        let upSourceSize = try XCTUnwrap(up["source"] as? [Int])
        let upSource = try XCTUnwrap(up["source_values"] as? [NSNumber]).map(\.floatValue)
        let upTarget = try XCTUnwrap(up["target"] as? [Int])
        let upExpected = try XCTUnwrap(up["values"] as? [NSNumber]).map(\.floatValue)
        let actualUp = MergeCore.pyrUp(upSource, width: upSourceSize[0], height: upSourceSize[1], toWidth: upTarget[0], toHeight: upTarget[1])
        assertClose(actualUp, upExpected, accuracy: 2e-6)

        let down = try XCTUnwrap(root["down"] as? [String: Any])
        let downSourceSize = try XCTUnwrap(down["source"] as? [Int])
        let downSource = try XCTUnwrap(down["source_values"] as? [NSNumber]).map(\.floatValue)
        let downTarget = try XCTUnwrap(down["target"] as? [Int])
        let downExpected = try XCTUnwrap(down["values"] as? [NSNumber]).map(\.floatValue)
        let actualDown = MergeCore.pyrDown(downSource, width: downSourceSize[0], height: downSourceSize[1])
        XCTAssertEqual(actualDown.width, downTarget[0])
        XCTAssertEqual(actualDown.height, downTarget[1])
        assertClose(actualDown.values, downExpected, accuracy: 2e-6)

        let even = try XCTUnwrap(root["upEven"] as? [String: Any])
        let evenSize = try XCTUnwrap(even["source"] as? [Int])
        let evenSource = try XCTUnwrap(even["source_values"] as? [NSNumber]).map(\.floatValue)
        let evenTarget = try XCTUnwrap(even["target"] as? [Int])
        let evenExpected = try XCTUnwrap(even["values"] as? [NSNumber]).map(\.floatValue)
        let actualEven = MergeCore.pyrUp(evenSource, width: evenSize[0], height: evenSize[1], toWidth: evenTarget[0], toHeight: evenTarget[1])
        assertClose(actualEven, evenExpected, accuracy: 2e-6)
    }

    func testPyramidLevelCountsForOddAndEvenDimensions() {
        XCTAssertEqual(MergeCore.pyramidLevelCount(width: 96, height: 96), 6)
        XCTAssertEqual(MergeCore.pyramidLevelCount(width: 7, height: 5), 2)
        XCTAssertEqual(MergeCore.pyramidLevelCount(width: 8, height: 6), 2)
    }

    func testLaplacianPyramidReconstructsForOddAndEvenShapes() {
        for (width, height) in [(7, 5), (8, 6)] {
            let source = (0..<(width * height)).map { Float(($0 * 17) % 101) / 100 }
            let lower = MergeCore.pyrDown(source, width: width, height: height)
            let expanded = MergeCore.pyrUp(lower.values, width: lower.width, height: lower.height, toWidth: width, toHeight: height)
            let laplacian = zip(source, expanded).map { $0.0 - $0.1 }
            let reconstructed = zip(laplacian, expanded).map { $0.0 + $0.1 }
            assertClose(reconstructed, source, accuracy: 1e-6)
        }
    }

    func testMergeUsesMiddlePivotAndAlignsKnownOnePixelShift() throws {
        let base = patternedImage(width: 96, height: 96)
        let shifted = translating(base, dx: 1, dy: -1)
        let result = try MergeCore.merge([base, shifted, base], edge: "keep")
        XCTAssertEqual(result.referenceIndex, 1)
        XCTAssertEqual(result.shiftsXY.count, 3)
        XCTAssertEqual(result.shiftsXY[0].0, 1); XCTAssertEqual(result.shiftsXY[0].1, -1)
        XCTAssertEqual(result.shiftsXY[1].0, 0); XCTAssertEqual(result.shiftsXY[1].1, 0)
        XCTAssertEqual(result.shiftsXY[2].0, 1); XCTAssertEqual(result.shiftsXY[2].1, -1)
        XCTAssertTrue(result.appliedShiftsXY.elementsEqual(result.shiftsXY, by: ==))
        XCTAssertTrue(result.aligned)
        XCTAssertEqual(result.crop, MergeCrop(x: 0, y: 0, width: 96, height: 96))
    }

    func testFlatImagesDoNotProduceNaNsAndFallBackUnaligned() throws {
        let flat = MergeRGBImage(width: 16, height: 16, pixels: Array(repeating: 0.4, count: 16 * 16 * 3))
        let result = try MergeCore.merge([flat, flat])
        XCTAssertFalse(result.aligned)
        XCTAssertEqual(result.reason, "align_failed")
        XCTAssertTrue(result.shiftsXY.allSatisfy { $0 == (0, 0) })
        XCTAssertTrue(result.appliedShiftsXY.allSatisfy { $0 == (0, 0) })
        XCTAssertTrue(result.image.pixels.allSatisfy { $0.isFinite && (0...1).contains($0) })
    }

    func testCropIsCommonAcrossShiftedFrames() throws {
        let base = patternedImage(width: 96, height: 96)
        let shifted = translating(base, dx: 1, dy: -1)
        let result = try MergeCore.merge([base, shifted], edge: "crop")
        XCTAssertEqual(result.crop, MergeCrop(x: 1, y: 0, width: 95, height: 95))
        XCTAssertTrue(result.appliedShiftsXY.elementsEqual(result.shiftsXY, by: ==))
        XCTAssertEqual(result.image.width, 95)
        XCTAssertEqual(result.image.height, 95)
    }

    func testThreeSyntheticBaselinesMeetPSNRTarget() throws {
        let baseURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/baseline")
        for group in 0..<3 {
            let images = try (0..<3).map { index in try readRGB(baseURL.appendingPathComponent("bracket-\(group)-\(index).png")) }
            let debugResult = try MergeCore.merge(images, edge: "keep")
            let expectedShifts: [[(Int, Int)]] = [
                [(-1, -1), (0, 0), (1, 1)],
                [(1, -1), (0, 0), (1, -1)],
                [(-1, 1), (0, 0), (-1, 1)],
            ]
            XCTAssertTrue(debugResult.shiftsXY.elementsEqual(expectedShifts[group], by: ==), "group \(group) MTB shifts")
            let actual = debugResult.image
            let reference = try readRGB(baseURL.appendingPathComponent("bracket-\(group)-merged.png"))
            XCTAssertEqual(actual.width, 96); XCTAssertEqual(actual.height, 96)
            let mse = zip(actual.pixels, reference.pixels).reduce(Float.zero) { $0 + ($1.0 - $1.1) * ($1.0 - $1.1) } / Float(actual.pixels.count)
            let psnr = 10 * log10(1 / Double(mse))
            let absoluteErrors = zip(actual.pixels, reference.pixels).map { abs($0.0 - $0.1) }.sorted()
            let meanAbsoluteError = absoluteErrors.reduce(Float.zero, +) / Float(absoluteErrors.count)
            let p95AbsoluteError = absoluteErrors[Int(Float(absoluteErrors.count - 1) * 0.95)]
            let maxAbsoluteError = absoluteErrors.last ?? 0
            XCTAssertGreaterThanOrEqual(psnr, 50, "group \(group) PSNR \(psnr)dB; MAE \(meanAbsoluteError), p95 abs \(p95AbsoluteError), max abs \(maxAbsoluteError); target is 50dB")
        }
    }

    func testFrameCountDimensionsAndFiniteValuesAreValidated() throws {
        let image = MergeRGBImage(width: 4, height: 4, pixels: Array(repeating: 0.5, count: 48))
        XCTAssertThrowsError(try MergeCore.merge([image]))
        XCTAssertThrowsError(try MergeCore.merge(Array(repeating: image, count: 10)))
        XCTAssertNoThrow(try MergeCore.merge(Array(repeating: image, count: 2)))
        XCTAssertNoThrow(try MergeCore.merge(Array(repeating: image, count: 9)))
        let compact = MergeRGBImage(width: 4, height: 4, uint16RGB: Array(repeating: 32_768, count: 48))
        XCTAssertNoThrow(try MergeCore.merge([compact, compact]))
        XCTAssertThrowsError(try MergeCore.merge([image, MergeRGBImage(width: 5, height: 4, pixels: Array(repeating: 0.5, count: 60))]))
        var invalid = image; invalid.pixels[3] = .nan
        XCTAssertThrowsError(try MergeCore.merge([image, invalid]))
        var negative = image; negative.pixels[3] = -0.01
        XCTAssertThrowsError(try MergeCore.merge([image, negative]))
        var tooLarge = image; tooLarge.pixels[3] = 1e30
        XCTAssertThrowsError(try MergeCore.merge([image, tooLarge]))
        let overflowing = MergeRGBImage(width: Int.max, height: 1, pixels: [])
        XCTAssertThrowsError(try MergeCore.merge([overflowing, overflowing]))
    }

    func testEmptyCommonCropFallsBackWithoutLosingDetectedShifts() throws {
        let base = patternedImage(width: 16, height: 16)
        let positive = translating(base, dx: 8, dy: 0)
        let negative = translating(base, dx: -8, dy: 0)
        let result = try MergeCore.merge([positive, base, negative], edge: "crop", maxShiftRatio: 1)
        XCTAssertFalse(result.aligned)
        XCTAssertEqual(result.reason, "empty_common_crop")
        XCTAssertEqual(result.crop, MergeCrop(x: 0, y: 0, width: 16, height: 16))
        XCTAssertTrue(result.shiftsXY.contains { $0 != (0, 0) })
        XCTAssertTrue(result.appliedShiftsXY.allSatisfy { $0 == (0, 0) })
    }

    private func assertClose(_ actual: [Float], _ expected: [Float], accuracy: Float, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(actual.count, expected.count, file: file, line: line)
        for (a, e) in zip(actual, expected) { XCTAssertEqual(a, e, accuracy: accuracy, file: file, line: line) }
    }

    private func readRGB(_ url: URL) throws -> MergeRGBImage {
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        var bytes = Array(repeating: UInt8.zero, count: image.width * image.height * 4)
        let context = try XCTUnwrap(CGContext(data: &bytes, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: image.width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        var pixels = Array(repeating: Float.zero, count: image.width * image.height * 3)
        for i in 0..<(image.width * image.height) { for c in 0..<3 { pixels[i * 3 + c] = Float(bytes[i * 4 + c]) / 255 } }
        return MergeRGBImage(width: image.width, height: image.height, pixels: pixels)
    }

    private func patternedImage(width: Int, height: Int) -> MergeRGBImage {
        var pixels = Array(repeating: Float.zero, count: width * height * 3)
        for y in 0..<height { for x in 0..<width {
            let v = Float(((x * 37 + y * 19 + (x * y) % 97) % 251)) / 250
            let i = (y * width + x) * 3
            pixels[i] = v; pixels[i + 1] = Float((x * 11 + y * 7) % 251) / 250; pixels[i + 2] = Float((x * 3 + y * 31) % 251) / 250
        } }
        return MergeRGBImage(width: width, height: height, pixels: pixels)
    }

    private func translating(_ image: MergeRGBImage, dx: Int, dy: Int) -> MergeRGBImage {
        var pixels = Array(repeating: Float.zero, count: image.pixels.count)
        for y in 0..<image.height { for x in 0..<image.width {
            let sx = x - dx, sy = y - dy
            guard sx >= 0, sy >= 0, sx < image.width, sy < image.height else { continue }
            for c in 0..<3 { pixels[(y * image.width + x) * 3 + c] = image.pixels[(sy * image.width + sx) * 3 + c] }
        } }
        return MergeRGBImage(width: image.width, height: image.height, pixels: pixels)
    }
}
