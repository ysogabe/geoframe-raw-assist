import CoreImage
import Foundation
import ImageIO

public struct MergeOptions: Equatable {
    public var align = true
    public var maxShiftRatio: Double = 0.02
    public var edge = "crop"
    public var weights = MergeWeights()
}

public struct MergeRequest: Equatable {
    public let inputs: [URL]
    public let output: URL
    public let maxEdge: Int?
    public let jpegQuality: Double
    public let decoderVersion: String?
    public let settings: Settings
    public let adjustments: Adjustments
    public let options: MergeOptions
}

public enum MergeMemoryBudget {
    /// Retained RGB UInt16 inputs plus one conservative fusion workspace allowance.
    /// The workspace is derived from MergeCore's current peak live buffers: current shifted
    /// RGB plane, weight planes, image/weight pyramids, Laplacian expansion scratch, accumulators,
    /// reconstruction/output, and per-frame decode/encode scratch. It is independent of N.
    public static let retainedBytesPerPixelPerFrame: UInt64 = 6
    public static let fixedWorkspaceBytesPerPixel: UInt64 = 112
    public static let fixedCeiling: UInt64 = 4_000_000_000
    public static func safeBytes(physicalMemory: UInt64) -> UInt64 {
        min(fixedCeiling, physicalMemory > 1_500_000_000 ? physicalMemory - 1_500_000_000 : 0)
    }
    public static func estimate(width: UInt64, height: UInt64, frames: Int, maxEdge: Int?) -> UInt64? {
        guard frames > 0, width > 0, height > 0 else { return nil }
        var w = width, h = height
        if let edge = maxEdge {
            let scale = min(1.0, Double(edge) / Double(max(width, height)))
            // CIRAWFilter's scaled dimensions can round up; use ceiling to avoid under-budgeting.
            let scaledWidth = (Double(width) * scale).rounded(.up)
            let scaledHeight = (Double(height) * scale).rounded(.up)
            guard scaledWidth.isFinite, scaledHeight.isFinite,
                  scaledWidth > 0, scaledHeight > 0,
                  scaledWidth < Double(UInt64.max), scaledHeight < Double(UInt64.max) else { return nil }
            w = UInt64(scaledWidth); h = UInt64(scaledHeight)
        }
        let (pixels, pixelOverflow) = w.multipliedReportingOverflow(by: h)
        let (retainedPerFrame, retainedBytesOverflow) = pixels.multipliedReportingOverflow(by: retainedBytesPerPixelPerFrame)
        let (retained, retainedOverflow) = retainedPerFrame.multipliedReportingOverflow(by: UInt64(frames))
        let (workspace, workspaceOverflow) = pixels.multipliedReportingOverflow(by: fixedWorkspaceBytesPerPixel)
        let (total, totalOverflow) = retained.addingReportingOverflow(workspace)
        return pixelOverflow || retainedBytesOverflow || retainedOverflow || workspaceOverflow || totalOverflow ? nil : total
    }
}

extension RequestParser {
    public static func isMergeRequest(_ data: Data) -> Bool {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return object["mode"] as? String == "merge"
    }

    public static func parseMerge(_ data: Data) throws -> MergeRequest {
        let object: Any
        do { object = try JSONSerialization.jsonObject(with: data) }
        catch { throw DevelopError(.invalidRequest, "request is not valid JSON") }
        guard let dict = object as? [String: Any] else { throw DevelopError(.invalidRequest, "request must be a JSON object") }
        try rejectUnknownKeys(dict, allowed: ["mode", "inputs", "output", "max_edge", "format", "jpeg_quality", "decoder_version", "settings", "adjustments", "merge"], context: "request")
        guard dict["mode"] as? String == "merge" else { throw DevelopError(.invalidRequest, "mode must be \"merge\"") }
        guard let values = dict["inputs"] as? [Any] else { throw DevelopError(.invalidRequest, "inputs must be an array of 2...9 absolute .ARW paths") }
        guard (2...9).contains(values.count) else { throw DevelopError(.invalidRequest, "inputs must contain 2...9 files") }
        let inputs = try values.map { value -> URL in
            guard let path = value as? String, !path.isEmpty, path.hasPrefix("/"), !path.contains("\0") else { throw DevelopError(.invalidRequest, "each inputs entry must be an absolute path") }
            let url = URL(fileURLWithPath: path).standardizedFileURL
            guard url.pathExtension.lowercased() == "arw" else { throw DevelopError(.invalidRequest, "inputs must be Sony .ARW files") }
            return url
        }
        guard Set(inputs.map { $0.standardizedFileURL.path }).count == inputs.count else { throw DevelopError(.invalidRequest, "inputs must not contain duplicate paths") }
        let output = try absolutePath(dict, "output")
        guard ["jpg", "jpeg"].contains(output.pathExtension.lowercased()) else { throw DevelopError(.invalidRequest, "output extension must be .jpg or .jpeg for merge") }
        if let format = dict["format"], !(format is NSNull), (format as? String) != "jpeg" { throw DevelopError(.invalidRequest, "merge supports format \"jpeg\" only") }
        if let raw = dict["output_color_space"], !(raw is NSNull) { throw DevelopError(.invalidRequest, "output_color_space is not supported in merge mode") }
        if let raw = dict["diagnostics"], !(raw is NSNull) { throw DevelopError(.invalidRequest, "diagnostics is not supported in merge mode") }
        var maxEdge: Int?
        if let raw = dict["max_edge"], !(raw is NSNull) {
            guard let edge = integer(raw), maxEdgeRange.contains(edge) else { throw DevelopError(.invalidRequest, "max_edge must be an integer in 64...20000") }
            maxEdge = edge
        }
        var quality = defaultJPEGQuality
        if let raw = dict["jpeg_quality"], !(raw is NSNull) {
            guard let q = number(raw), (0...1).contains(q) else { throw DevelopError(.invalidRequest, "jpeg_quality must be a number in 0...1") }
            quality = q
        }
        let decoder = try parseDecoderVersion(dict)
        let settings = try parseSettings(dict["settings"])
        if settings.exposureEV != 0 { throw DevelopError(.invalidRequest, "merge requires settings.exposure_ev to be 0") }
        let adjustments = try parseAdjustments(dict["adjustments"]) ?? { throw DevelopError(.invalidRequest, "merge requires all 9 adjustment keys") }()
        try validateAdjustmentsAgainstSettings(dict["settings"], settings)
        var options = MergeOptions()
        if let raw = dict["merge"], !(raw is NSNull) {
            guard let m = raw as? [String: Any] else { throw DevelopError(.invalidRequest, "merge must be an object") }
            try rejectUnknownKeys(m, allowed: ["align", "max_shift_ratio", "edge", "weights"], context: "merge")
            if let v = m["align"] { guard let b = boolean(v) else { throw DevelopError(.invalidRequest, "merge.align must be boolean") }; options.align = b }
            if let v = m["max_shift_ratio"] { guard let x = number(v), (0...1).contains(x) else { throw DevelopError(.invalidRequest, "merge.max_shift_ratio must be a finite number in 0...1") }; options.maxShiftRatio = x }
            if let v = m["edge"] { guard let s = v as? String, ["crop", "keep"].contains(s) else { throw DevelopError(.invalidRequest, "merge.edge must be crop or keep") }; options.edge = s }
            if let v = m["weights"] {
                guard let w = v as? [String: Any] else { throw DevelopError(.invalidRequest, "merge.weights must be an object") }
                try rejectUnknownKeys(w, allowed: ["contrast", "saturation", "exposure"], context: "merge.weights")
                func weight(_ key: String, _ fallback: Float) throws -> Float {
                    guard let raw = w[key] else { return fallback }
                    guard let x = number(raw), (0...3).contains(x) else { throw DevelopError(.invalidRequest, "merge.weights.\(key) must be a finite number in 0...3") }
                    return Float(x)
                }
                options.weights = MergeWeights(contrast: try weight("contrast", 1), saturation: try weight("saturation", 1), exposure: try weight("exposure", 0))
            }
        }
        return MergeRequest(inputs: inputs, output: output, maxEdge: maxEdge, jpegQuality: quality, decoderVersion: decoder, settings: settings, adjustments: adjustments, options: options)
    }
}

public struct MergeElapsed: Encodable { public let decode, align, fuse, post_encode, total: Double }
public struct MergeFrame: Encodable { public let input: String; public let width: Int; public let height: Int; public let applied: Applied; public let source: SourceInfo }
public struct MergeMetadata: Encodable {
    public let algorithm = "mtb+mertens"; public let merge_version = 1; public let aligned: Bool; public let reason: String?
    public let reference_index: Int; public let shifts_xy: [[Int]]; public let applied_shifts_xy: [[Int]]; public let crop: MergeCrop
    public let max_bits: Int; public let max_shift_px: Int; public let pyramid_levels: Int; public let weights: MergeWeights; public let edge: String
}
public struct MergeSuccess: Encodable {
    public let ok = true; public let engine = "coreimage"; public let cli_version: String; public let macos_version: String; public let decoder_version: String
    public let mode = "merge"; public let format = "jpeg"; public let inputs: [String]; public let output: String; public let width: Int; public let height: Int
    public let elapsed_ms: MergeElapsed; public let merge: MergeMetadata; public let frames: [MergeFrame]; public let adjustments_applied: AdjustmentsApplied
    public var sequence: Int? = nil
}

private func ms(_ start: DispatchTime, _ end: DispatchTime) -> Double { Double(end.uptimeNanoseconds - start.uptimeNanoseconds) / 1_000_000 }

public func merge(_ request: MergeRequest, context sharedContext: CIContext? = nil) throws -> MergeSuccess {
    try PathValidator.validate(request)
    let start = DispatchTime.now()
    // Bound retained frames + Mertens working buffers before decoding any RAW. Leave at least 1 GiB for Core Image/system.
    let available = ProcessInfo.processInfo.physicalMemory
    let safeBudget = MergeMemoryBudget.safeBytes(physicalMemory: available)
    var metadataDimensions: [(UInt64, UInt64)] = []
    for url in request.inputs {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil), let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let w = (props[kCGImagePropertyPixelWidth] as? NSNumber)?.uint64Value, let h = (props[kCGImagePropertyPixelHeight] as? NSNumber)?.uint64Value else {
            throw DevelopError(.inputNotReadable, "cannot read RAW dimensions: \(url.path)")
        }
        metadataDimensions.append((w, h))
    }
    // Frames must decode to a common extent. Until that is known, budget the largest metadata
    // extent as N retained RGB16 frames plus one fixed (N-independent) MergeCore workspace.
    let maxWidth = metadataDimensions.map(\.0).max()!, maxHeight = metadataDimensions.map(\.1).max()!
    guard let estimated = MergeMemoryBudget.estimate(width: maxWidth, height: maxHeight, frames: request.inputs.count, maxEdge: request.maxEdge) else {
        throw DevelopError(.invalidRequest, "estimated merge memory overflows")
    }
    if estimated > safeBudget { throw DevelopError(.invalidRequest, "estimated merge working memory \(estimated) bytes exceeds safe budget \(safeBudget) bytes") }
    let context = sharedContext ?? makeRenderContext()
    let decodeStart = DispatchTime.now()
    var retained: [MergeRGBImage] = []; var frames: [MergeFrame] = []; var firstSize: (Int, Int)?
    let effective = Adjustments(exposure: 0, wbTemp: request.adjustments.wbTemp, shadowLift: request.adjustments.shadowLift, highlightRecovery: 0, brightness: 1, contrast: 1, saturation: 1, sharpness: request.adjustments.sharpness, noiseReduction: request.adjustments.noiseReduction)
    var firstPlan: AdjustmentsDecoderPlan?
    for input in request.inputs {
        try autoreleasepool {
            let configured = try configureRAWFilter(input: input, maxEdge: request.maxEdge, decoderVersion: request.decoderVersion, settings: request.settings, adjustments: effective)
            configured.filter.exposure = 0
            guard let decoded = configured.filter.outputImage, isUsableExtent(decoded.extent) else { throw DevelopError(.renderFailed, "CIRAWFilter produced invalid output") }
            let extent = decoded.extent.integral; let width = Int(extent.width); let height = Int(extent.height)
            if let size = firstSize, size != (width, height) { throw DevelopError(.invalidRequest, "frames differ in size or orientation") }
            firstSize = (width, height)
            let (px, overflow) = width.multipliedReportingOverflow(by: height); guard !overflow else { throw DevelopError(.invalidRequest, "decoded image dimensions overflow") }
            guard width > 0, height > 0,
                  let decodedEstimate = MergeMemoryBudget.estimate(width: UInt64(width), height: UInt64(height), frames: request.inputs.count, maxEdge: nil),
                  decodedEstimate <= safeBudget else {
                throw DevelopError(.invalidRequest, "decoded merge extent exceeds safe memory budget")
            }
            let (rgbaCount, rgbaOverflow) = px.multipliedReportingOverflow(by: 4)
            let (rgbCount, rgbOverflow) = px.multipliedReportingOverflow(by: 3)
            let (rowBytes, rowOverflow) = width.multipliedReportingOverflow(by: 8)
            guard !rgbaOverflow, !rgbOverflow, !rowOverflow else { throw DevelopError(.invalidRequest, "decoded image buffer dimensions overflow") }
            var samples = Array(repeating: UInt16(0), count: rgbaCount)
            let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
            samples.withUnsafeMutableBytes { buffer in context.render(decoded, toBitmap: buffer.baseAddress!, rowBytes: rowBytes, bounds: extent, format: .RGBA16, colorSpace: colorSpace) }
            var rgb = Array(repeating: UInt16(0), count: rgbCount)
            for p in 0..<px { rgb[p*3] = samples[p*4]; rgb[p*3+1] = samples[p*4+1]; rgb[p*3+2] = samples[p*4+2] }
            retained.append(MergeRGBImage(width: width, height: height, uint16RGB: rgb))
            let applied = makeApplied(configured.filter, profile: request.settings.profile, format: .jpeg, colorSpaceName: "sRGB IEC61966-2.1")
            let source = makeSourceInfo(configured.filter, native: configured.native)
            frames.append(MergeFrame(input: input.path, width: width, height: height, applied: applied, source: source))
            if firstPlan == nil { firstPlan = configured.plan }
        }
    }
    let decodeEnd = DispatchTime.now()
    let result = try MergeCore.merge(retained, weights: request.options.weights, edge: request.options.edge, maxShiftRatio: Float(request.options.maxShiftRatio), align: request.options.align)
    let postStart = DispatchTime.now()
    let linear = mergePostChainInput(result.image)
    let postSettings = request.adjustments
    let base = captureSharpenBase(longEdge: max(result.image.width, result.image.height), iso: frames.first?.source.iso.map(Double.init))
    let post = try PostChain.apply(linear, PostChainParameters(lift: postSettings.shadowLift / 100, brightness: postSettings.brightness, contrast: postSettings.contrast, saturation: postSettings.saturation, usmSigma: base.sigma, usmGain: usmGainForCoreImage(sharpness: postSettings.sharpness, base: base), usmCoring: base.coring))
    let outputExtent = post.extent.integral
    guard let image = context.createCGImage(post, from: outputExtent, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!) else { throw DevelopError(.renderFailed, "Core Image failed to render merged image") }
    try AtomicImageWriter.write(image, to: request.output, format: .jpeg, jpegQuality: request.jpegQuality)
    let end = DispatchTime.now()
    var reportedAdjustments = postSettings
    reportedAdjustments.exposure = 0
    reportedAdjustments.highlightRecovery = 0
    let resolvedPost = HighlightRecoveryMapping.resolve(reportedAdjustments)
    let defaults = firstPlan?.defaults ?? DecoderDefaults(sharpnessAmount: 0, luminanceNoiseReduction: 0, colorNoiseReduction: 0, neutralTemperature: 0, neutralTint: 0)
    let appliedAdjustments = AdjustmentsApplied(
        defaults: defaults.encodable,
        post: AdjustmentsApplied.Post(
            shadow_lift: postSettings.shadowLift / 100, brightness: postSettings.brightness, contrast: postSettings.contrast,
            saturation: postSettings.saturation,
            usm: AdjustmentsApplied.USM(sigma: base.sigma, gain: usmGainForCoreImage(sharpness: postSettings.sharpness, base: base), coring: base.coring, long_edge: max(result.image.width, result.image.height), iso: frames.first?.source.iso),
            wb: AdjustmentsApplied.WB(k0: defaults.neutralTemperature, mired_shift: firstPlan?.miredShift ?? 0, mired_per_unit: WhiteBalanceMapping.miredPerUnit),
            noise_reduction: postSettings.noiseReduction.rawValue
        ),
        highlight_recovery: AdjustmentsApplied.HighlightRecovery(resolvedPost.effect), ignored: ["exposure", "highlight_recovery"], guards: firstPlan?.guards ?? []
    )
    let meta = MergeMetadata(aligned: request.options.align && result.aligned, reason: result.reason, reference_index: result.referenceIndex, shifts_xy: result.shiftsXY.map { [$0.0, $0.1] }, applied_shifts_xy: result.appliedShiftsXY.map { [$0.0, $0.1] }, crop: result.crop, max_bits: result.maxBits, max_shift_px: result.maxShiftPx, pyramid_levels: result.pyramidLevels, weights: request.options.weights, edge: request.options.edge)
    return MergeSuccess(cli_version: cliVersion, macos_version: macOSVersionString, decoder_version: frames.first?.applied.decoder_version ?? "9", inputs: request.inputs.map(\.path), output: request.output.path, width: image.width, height: image.height, elapsed_ms: MergeElapsed(decode: ms(decodeStart, decodeEnd), align: result.alignmentDurationMS, fuse: result.fusionDurationMS, post_encode: ms(postStart, end), total: ms(start, end)), merge: meta, frames: frames, adjustments_applied: appliedAdjustments)
}

/// Build the exact sRGB-tagged CIImage input used by merge's shared PostChain path.
/// Core Image color management converts the tagged source into the context working space.
func mergePostChainInput(_ image: MergeRGBImage) -> CIImage {
    let raw = image.pixels
    let rgba = raw.enumerated().reduce(into: [Float]()) { acc, item in
        if item.offset % 3 == 0 { acc.append(item.element); acc.append(raw[item.offset + 1]); acc.append(raw[item.offset + 2]); acc.append(1) }
    }
    let data = rgba.withUnsafeBufferPointer { Data(buffer: $0) }
    return CIImage(bitmapData: data, bytesPerRow: image.width * 16,
                   size: CGSize(width: image.width, height: image.height), format: .RGBAf,
                   colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
}
