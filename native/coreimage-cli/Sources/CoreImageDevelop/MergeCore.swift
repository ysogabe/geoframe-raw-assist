import Foundation

/// Contiguous, interleaved RGB pixels in sRGB gamma space, normalized to 0...1.
public struct MergeRGBImage: Equatable {
    public let width: Int
    public let height: Int
    private var floatStorage: [Float]?
    private var uint16Storage: [UInt16]?

    /// Float32 storage for callers whose decoder already returns normalized RGB.
    public var pixels: [Float] {
        get { floatStorage ?? uint16Storage!.map { Float($0) / 65_535 } }
        set { floatStorage = newValue; uint16Storage = nil }
    }

    public init(width: Int, height: Int, pixels: [Float]) {
        self.width = width
        self.height = height
        self.floatStorage = pixels
        self.uint16Storage = nil
    }

    /// Compact retained storage for full-resolution brackets. Values are linear quantizations of 0...1 samples.
    public init(width: Int, height: Int, uint16RGB: [UInt16]) {
        self.width = width
        self.height = height
        self.floatStorage = nil
        self.uint16Storage = uint16RGB
    }

    fileprivate var hasValidPixels: Bool {
        if let floatStorage { return floatStorage.count == width * height * 3 && floatStorage.allSatisfy(\.isFinite) }
        return uint16Storage?.count == width * height * 3
    }
}

public struct MergeWeights: Equatable {
    public var contrast: Float
    public var saturation: Float
    public var exposure: Float
    public init(contrast: Float = 1, saturation: Float = 1, exposure: Float = 0) {
        self.contrast = contrast
        self.saturation = saturation
        self.exposure = exposure
    }
}

public struct MergeCrop: Equatable {
    public let x: Int
    public let y: Int
    public let width: Int
    public let height: Int

    public init(x: Int, y: Int, width: Int, height: Int) {
        self.x = x; self.y = y; self.width = width; self.height = height
    }
}

public struct MergeResult: Equatable {
    public let image: MergeRGBImage
    public let shiftsXY: [(Int, Int)]
    public let referenceIndex: Int
    public let crop: MergeCrop
    public let pyramidLevels: Int
    public let aligned: Bool
    public let reason: String?

    public static func == (lhs: MergeResult, rhs: MergeResult) -> Bool {
        lhs.image == rhs.image && lhs.shiftsXY.elementsEqual(rhs.shiftsXY, by: ==)
            && lhs.referenceIndex == rhs.referenceIndex && lhs.crop == rhs.crop
            && lhs.pyramidLevels == rhs.pyramidLevels && lhs.aligned == rhs.aligned && lhs.reason == rhs.reason
    }
}

/// CPU-only AlignMTB + Mertens exposure fusion. This file deliberately has no CoreImage/ImageIO imports.
public enum MergeCore {
    public static let minimumFrames = 2
    public static let maximumFrames = 9

    private struct Plane {
        var width: Int
        var height: Int
        var channels: Int
        var values: [Float]
        subscript(_ x: Int, _ y: Int, _ c: Int) -> Float {
            get { values[(y * width + x) * channels + c] }
            set { values[(y * width + x) * channels + c] = newValue }
        }
    }

    private static let kernel: [Float] = [1 / 16, 4 / 16, 6 / 16, 4 / 16, 1 / 16]

    public static func merge(_ images: [MergeRGBImage], weights: MergeWeights = .init(), edge: String = "crop", maxShiftRatio: Float = 0.02) throws -> MergeResult {
        guard (minimumFrames...maximumFrames).contains(images.count) else { throw MergeError.invalidFrameCount(images.count) }
        guard let first = images.first, first.width > 0, first.height > 0 else { throw MergeError.invalidImage }
        guard edge == "crop" || edge == "keep" else { throw MergeError.invalidOptions }
        guard maxShiftRatio.isFinite, maxShiftRatio >= 0, maxShiftRatio <= 1,
              [weights.contrast, weights.saturation, weights.exposure].allSatisfy({ $0.isFinite && $0 >= 0 && $0 <= 3 }) else { throw MergeError.invalidOptions }
        let (pixelCount, pixelOverflow) = first.width.multipliedReportingOverflow(by: first.height)
        let (rgbCount, rgbOverflow) = pixelCount.multipliedReportingOverflow(by: 3)
        guard !pixelOverflow, !rgbOverflow else { throw MergeError.invalidImage }
        for image in images {
            guard image.width == first.width, image.height == first.height else { throw MergeError.dimensionMismatch }
            guard image.hasValidPixels, image.width * image.height * 3 == rgbCount else { throw MergeError.invalidImage }
        }

        let pivot = images.count / 2
        let maxBits = max(1, min(6, Int(floor(log2(Double(max(2, Int(Float(min(first.width, first.height)) * maxShiftRatio))))))))
        var shifts = Array(repeating: (0, 0), count: images.count)
        var alignFailed = false
        if images.count > 1 {
            for i in images.indices where i != pivot {
                guard let shift = mtbShift(images[i], reference: images[pivot], maxBits: maxBits) else { alignFailed = true; continue }
                shifts[i] = shift
            }
        }
        let shortEdge = Float(min(first.width, first.height))
        let exceeds = shifts.contains { abs(Float($0.0)) > shortEdge * maxShiftRatio || abs(Float($0.1)) > shortEdge * maxShiftRatio }
        let fallback = alignFailed || exceeds
        if fallback { shifts = Array(repeating: (0, 0), count: images.count) }
        let crop = edge == "crop" && !fallback ? commonCrop(width: first.width, height: first.height, shifts: shifts) : MergeCrop(x: 0, y: 0, width: first.width, height: first.height)
        let aligned = !fallback
        let levels = pyramidLevelCount(width: crop.width, height: crop.height)
        let output = mertens(images, shifts: shifts, crop: crop, weights: weights, levels: levels)
        return MergeResult(image: MergeRGBImage(width: crop.width, height: crop.height, pixels: output), shiftsXY: shifts, referenceIndex: pivot, crop: crop, pyramidLevels: levels, aligned: aligned, reason: fallback ? (exceeds ? "shift_exceeds_ratio" : "align_failed") : nil)
    }

    public enum MergeError: Error, Equatable {
        case invalidFrameCount(Int), invalidImage, dimensionMismatch, invalidOptions
    }

    public static func pyramidLevelCount(width: Int, height: Int) -> Int {
        guard min(width, height) > 1 else { return 0 }
        return Int(floor(log2(Double(min(width, height)))))
    }

    /// OpenCV pyrDown: separable 5-tap Gaussian, reflect-101, then even samples.
    public static func pyrDown(_ plane: [Float], width: Int, height: Int, channels: Int = 1) -> (values: [Float], width: Int, height: Int) {
        let outW = (width + 1) / 2, outH = (height + 1) / 2
        var horizontal = Array(repeating: Float.zero, count: width * height * channels)
        for y in 0..<height { for x in 0..<width { for c in 0..<channels {
            var sum: Float = 0
            for k in -2...2 { sum += plane[(y * width + reflect101(x + k, width)) * channels + c] * kernel[k + 2] }
            horizontal[(y * width + x) * channels + c] = sum
        } } }
        var output = Array(repeating: Float.zero, count: outW * outH * channels)
        for y in 0..<outH { for x in 0..<outW { for c in 0..<channels {
            var sum: Float = 0
            for k in -2...2 { sum += horizontal[(reflect101(2 * y + k, height) * width + 2 * x) * channels + c] * kernel[k + 2] }
            output[(y * outW + x) * channels + c] = sum
        } } }
        return (output, outW, outH)
    }

    /// OpenCV pyrUp's odd-size phase: filtering is indexed in the source grid before truncation.
    public static func pyrUp(_ plane: [Float], width: Int, height: Int, toWidth: Int, toHeight: Int, channels: Int = 1) -> [Float] {
        let outW = toWidth, outH = toHeight
        var horizontal = Array(repeating: Float.zero, count: outW * height * channels)
        for y in 0..<height { for x in 0..<outW { for c in 0..<channels {
            let v: Float
            if outW == width * 2 {
                var sum: Float = 0
                for k in -2...2 {
                    let px = reflect101(x + k, outW)
                    if px.isMultiple(of: 2) { sum += plane[(y * width + px / 2) * channels + c] * kernel[k + 2] * 2 }
                }
                v = sum
            } else {
                let i = x / 2
                if x.isMultiple(of: 2) {
                    let right = x == outW - 1 ? i : reflect101(i + 1, width)
                    v = (plane[(y * width + reflect101(i - 1, width)) * channels + c] + 6 * plane[(y * width + i) * channels + c] + plane[(y * width + right) * channels + c]) * 0.125
                } else {
                    v = (plane[(y * width + i) * channels + c] + plane[(y * width + reflect101(i + 1, width)) * channels + c]) * 0.5
                }
            }
            horizontal[(y * outW + x) * channels + c] = v
        } } }
        var output = Array(repeating: Float.zero, count: outW * outH * channels)
        for y in 0..<outH { for x in 0..<outW { for c in 0..<channels {
            let value: Float
            if outH == height * 2 {
                var sum: Float = 0
                for k in -2...2 {
                    let py = reflect101(y + k, outH)
                    if py.isMultiple(of: 2) { sum += horizontal[(py / 2 * outW + x) * channels + c] * kernel[k + 2] * 2 }
                }
                value = sum
            } else {
                let i = y / 2
                if y.isMultiple(of: 2) {
                    let below = y == outH - 1 ? i : reflect101(i + 1, height)
                    value = (horizontal[(reflect101(i - 1, height) * outW + x) * channels + c] + 6 * horizontal[(i * outW + x) * channels + c] + horizontal[(below * outW + x) * channels + c]) * 0.125
                } else {
                    let below = y == outH - 1 ? i : reflect101(i + 1, height)
                    value = (horizontal[(i * outW + x) * channels + c] + horizontal[(below * outW + x) * channels + c]) * 0.5
                }
            }
            output[(y * outW + x) * channels + c] = value
        } } }
        return output
    }

    private static func reflect101(_ i: Int, _ n: Int) -> Int {
        if n <= 1 { return 0 }
        var x = i
        while x < 0 || x >= n { x = x < 0 ? -x : 2 * n - x - 2 }
        return x
    }

    private static func gray(_ image: MergeRGBImage) -> Plane {
        let pixels = image.pixels
        var p = Plane(width: image.width, height: image.height, channels: 1, values: Array(repeating: 0, count: image.width * image.height))
        for i in 0..<p.values.count { p.values[i] = 255 * (0.299 * pixels[i * 3] + 0.587 * pixels[i * 3 + 1] + 0.114 * pixels[i * 3 + 2]) }
        return p
    }

    private static func mtbShift(_ image: MergeRGBImage, reference: MergeRGBImage, maxBits: Int) -> (Int, Int)? {
        var src = gray(image), ref = gray(reference)
        let maxLevel = max(0, Int(floor(log2(Double(max(src.width, src.height))))) - 1)
        let levels = min(maxLevel, maxBits - 1)
        var srcP = [src], refP = [ref]
        if levels > 0 { for _ in 0..<levels { let a = pyrDown(srcP.last!.values, width: srcP.last!.width, height: srcP.last!.height); let b = pyrDown(refP.last!.values, width: refP.last!.width, height: refP.last!.height); srcP.append(Plane(width: a.width, height: a.height, channels: 1, values: a.values)); refP.append(Plane(width: b.width, height: b.height, channels: 1, values: b.values)) } }
        var shift = (0, 0)
        for level in stride(from: levels, through: 0, by: -1) {
            src = srcP[level]; ref = refP[level]
            if level != levels { shift = (shift.0 * 2, shift.1 * 2) }
            let sm = medianMask(src.values), rm = medianMask(ref.values)
            var best = Int.max, candidate = shift
            for dy in -1...1 { for dx in -1...1 {
                let sx = shift.0 + dx, sy = shift.1 + dy
                var score = 0, samples = 0
                for y in 0..<src.height { for x in 0..<src.width {
                    let rx = x + sx, ry = y + sy
                    if rx < 0 || ry < 0 || rx >= ref.width || ry >= ref.height { continue }
                    let a = y * src.width + x, b = ry * ref.width + rx
                    if sm.valid[a] && rm.valid[b] { score += sm.threshold[a] != rm.threshold[b] ? 1 : 0; samples += 1 }
                } }
                if samples > 0 && score < best { best = score; candidate = (sx, sy) }
            } }
            if best == Int.max { return nil }
            shift = candidate
        }
        return shift
    }

    private static func medianMask(_ values: [Float]) -> (threshold: [Bool], valid: [Bool]) {
        var histogram = Array(repeating: 0, count: 256)
        for value in values { histogram[min(255, max(0, Int(value.rounded())))] += 1 }
        let target = values.count / 2
        var sum = 0, median = 0
        for i in histogram.indices { sum += histogram[i]; if sum > target { median = i; break } }
        return (values.map { $0 > Float(median) }, values.map { abs($0 - Float(median)) > 4 })
    }

    private static func commonCrop(width: Int, height: Int, shifts: [(Int, Int)]) -> MergeCrop {
        let x0 = max(0, shifts.map(\.0).max() ?? 0), x1 = width + min(0, shifts.map(\.0).min() ?? 0)
        let y0 = max(0, shifts.map(\.1).max() ?? 0), y1 = height + min(0, shifts.map(\.1).min() ?? 0)
        return MergeCrop(x: x0, y: y0, width: max(1, x1 - x0), height: max(1, y1 - y0))
    }

    private static func shiftedCrop(_ image: MergeRGBImage, shift: (Int, Int), crop: MergeCrop) -> Plane {
        let pixels = image.pixels
        var p = Plane(width: crop.width, height: crop.height, channels: 3, values: Array(repeating: 0, count: crop.width * crop.height * 3))
        for y in 0..<crop.height { for x in 0..<crop.width {
            let sx = x + crop.x - shift.0, sy = y + crop.y - shift.1
            guard sx >= 0, sy >= 0, sx < image.width, sy < image.height else { continue }
            for c in 0..<3 { p[x, y, c] = pixels[(sy * image.width + sx) * 3 + c] }
        } }
        return p
    }

    private static func mertens(_ sourceImages: [MergeRGBImage], shifts: [(Int, Int)], crop: MergeCrop, weights: MergeWeights, levels: Int) -> [Float] {
        let w = crop.width, h = crop.height
        var weightSum = Array(repeating: Float.zero, count: w * h)
        for index in sourceImages.indices {
            let image = shiftedCrop(sourceImages[index], shift: shifts[index], crop: crop)
            let weight = makeWeight(image, weights: weights)
            for i in weightSum.indices { weightSum[i] += weight[i] }
        }
        var accum = (0...levels).map { level -> Plane in
            let scale = 1 << level
            let pw = max(1, (w + scale - 1) / scale), ph = max(1, (h + scale - 1) / scale)
            return Plane(width: pw, height: ph, channels: 3, values: Array(repeating: 0, count: pw * ph * 3))
        }
        for index in sourceImages.indices {
            let image = shiftedCrop(sourceImages[index], shift: shifts[index], crop: crop)
            var imageP = [image], weightP = [Plane(width: w, height: h, channels: 1, values: makeWeight(image, weights: weights).enumerated().map { i, value in value / max(weightSum[i], 1e-12) })]
            if levels > 0 { for _ in 0..<levels {
                let a = pyrDown(imageP.last!.values, width: imageP.last!.width, height: imageP.last!.height, channels: 3)
                let b = pyrDown(weightP.last!.values, width: weightP.last!.width, height: weightP.last!.height)
                imageP.append(Plane(width: a.width, height: a.height, channels: 3, values: a.values)); weightP.append(Plane(width: b.width, height: b.height, channels: 1, values: b.values))
            } }
            for level in 0..<levels {
                let up = pyrUp(imageP[level + 1].values, width: imageP[level + 1].width, height: imageP[level + 1].height, toWidth: imageP[level].width, toHeight: imageP[level].height, channels: 3)
                for i in imageP[level].values.indices { imageP[level].values[i] -= up[i] }
            }
            for level in 0...levels { let lap = imageP[level].values, weightsPlane = weightP[level].values
                for pixel in weightsPlane.indices { for c in 0..<3 { accum[level].values[pixel * 3 + c] += lap[pixel * 3 + c] * weightsPlane[pixel] } }
            }
        }
        var result = accum[levels].values
        if levels > 0 { for level in stride(from: levels - 1, through: 0, by: -1) {
            result = pyrUp(result, width: accum[level + 1].width, height: accum[level + 1].height, toWidth: accum[level].width, toHeight: accum[level].height, channels: 3)
            for i in result.indices { result[i] += accum[level].values[i] }
        } }
        for i in result.indices { result[i] = min(1, max(0, result[i].isFinite ? result[i] : 0)) }
        return result
    }

    private static func makeWeight(_ image: Plane, weights: MergeWeights) -> [Float] {
        var gray = Array(repeating: Float.zero, count: image.width * image.height)
        var saturation = gray
        for i in gray.indices {
            let r = image.values[i * 3], g = image.values[i * 3 + 1], b = image.values[i * 3 + 2]
            gray[i] = 0.299 * r + 0.587 * g + 0.114 * b
            let mean = (r + g + b) / 3
            saturation[i] = sqrt((r - mean) * (r - mean) + (g - mean) * (g - mean) + (b - mean) * (b - mean))
        }
        var contrast = gray
        for y in 0..<image.height { for x in 0..<image.width {
            let center = gray[y * image.width + x]
            let lap = -4 * center + gray[reflect101(y - 1, image.height) * image.width + x] + gray[reflect101(y + 1, image.height) * image.width + x] + gray[y * image.width + reflect101(x - 1, image.width)] + gray[y * image.width + reflect101(x + 1, image.width)]
            contrast[y * image.width + x] = abs(lap)
        } }
        return gray.indices.map { i in
            var weight: Float = 1
            if weights.contrast > 0 { weight *= pow(contrast[i], weights.contrast) }
            if weights.saturation > 0 { weight *= pow(saturation[i], weights.saturation) }
            if weights.exposure > 0 { let e = exp(-((image.values[i * 3] - 0.5) * (image.values[i * 3] - 0.5) + (image.values[i * 3 + 1] - 0.5) * (image.values[i * 3 + 1] - 0.5) + (image.values[i * 3 + 2] - 0.5) * (image.values[i * 3 + 2] - 0.5)) / 0.08); weight *= pow(e, weights.exposure) }
            return weight + 1e-12
        }
    }
}
