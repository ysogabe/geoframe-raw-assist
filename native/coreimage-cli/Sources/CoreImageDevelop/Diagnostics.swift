import CoreImage
import Foundation

// 診断モード（CLI 0.4.0、Issue #92）。設計書 docs/design/coreimage-highlight-diagnosis.md §4。
// ハイライトの白飛びを段階別に切り分けるための観測専用の機能で、**アプリ（本番）からは使わない**。
// `diagnostics` を指定しないリクエストは、ここにあるコードを一切通らない（0.3.0 と同じ経路・出力）。

// MARK: - リクエスト

/// 統計・dump を描画する extended linear 色空間。
public enum DiagnosticsColorSpace: String, Equatable {
    case linearSRGB = "linear_srgb"
    case linearP3 = "linear_p3"

    var cgColorSpace: CGColorSpace {
        switch self {
        case .linearSRGB:
            return CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!
        case .linearP3:
            return CGColorSpace(name: CGColorSpace.extendedLinearDisplayP3)!
        }
    }
}

/// `diagnostics.dump_float`。clamp しない float32 RGB の生ダンプ（`.f32`）。
public struct DumpFloatRequest: Equatable {
    public var path: URL
    public var colorSpace: DiagnosticsColorSpace = .linearSRGB
}

public enum LinearSpaceFilterKind: String, Equatable {
    case identity
    case scale
    case constant
}

/// `diagnostics.linear_space_filter`。CIRAWFilter.linearSpaceFilter に挿す CIColorMatrix。
public struct LinearSpaceFilterRequest: Equatable {
    public var kind: LinearSpaceFilterKind
    /// scale: 係数、constant: 置換値。identity では nil。
    public var value: Double?

    public static let scaleRange = 0.01...8.0
    public static let constantRange = 0.0...8.0

    /// (scale, bias)。出力 = 入力 × scale + bias（RGB。アルファは素通し）。
    var matrix: (scale: Double, bias: Double) {
        switch kind {
        case .identity: return (1, 0)
        case .scale: return (value ?? 1, 0)
        case .constant: return (0, value ?? 0)
        }
    }

    func makeFilter() -> CIFilter {
        let (scale, bias) = matrix
        let s = CGFloat(scale)
        let b = CGFloat(bias)
        let filter = CIFilter(name: "CIColorMatrix")!
        filter.setDefaults()
        filter.setValue(CIVector(x: s, y: 0, z: 0, w: 0), forKey: "inputRVector")
        filter.setValue(CIVector(x: 0, y: s, z: 0, w: 0), forKey: "inputGVector")
        filter.setValue(CIVector(x: 0, y: 0, z: s, w: 0), forKey: "inputBVector")
        filter.setValue(CIVector(x: 0, y: 0, z: 0, w: 1), forKey: "inputAVector")
        filter.setValue(CIVector(x: b, y: b, z: b, w: 0), forKey: "inputBiasVector")
        return filter
    }
}

/// post_filters のパラメータ値（数値 or ベクトル）。
public enum FilterParameterValue: Equatable, Encodable {
    case number(Double)
    case vector([Double])

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .number(let value): try container.encode(value)
        case .vector(let values): try container.encode(values)
        }
    }
}

/// `diagnostics.post_filters` の 1 要素（許可リストで検証済み）。
public struct PostFilterRequest: Equatable {
    public var name: String
    public var parameters: [String: FilterParameterValue]
}

/// 検証済みの `diagnostics`。
public struct Diagnostics: Equatable {
    public var stats: Bool = true
    public var statsColorSpace: DiagnosticsColorSpace = .linearSRGB
    public var dumpFloat: DumpFloatRequest?
    public var linearSpaceFilter: LinearSpaceFilterRequest?
    public var postFilters: [PostFilterRequest]?
    public var skipWrite: Bool = false

    public init() {}
}

// MARK: - post_filters の許可リスト（設計書 §4.5）

enum PostFilterParameterKind {
    /// 数値（値域つき）。
    case number(ClosedRange<Double>)
    /// [x, y]（CIToneCurve の制御点）。
    case point(x: ClosedRange<Double>, y: ClosedRange<Double>)
    /// [r, g, b, a]（有限の数値）。
    case components4
}

public enum PostFilterAllowList {
    /// `--capabilities` に載せる順。
    public static let names = ["CIHighlightShadowAdjust", "CIToneCurve", "CIExposureAdjust", "CIColorClamp"]
    /// post_filters の最大個数（診断用の上限。暴走したリクエストを弾くため）。
    public static let maxFilters = 16

    static let toneCurvePoints = (0...4).map { "inputPoint\($0)" }
    /// CIToneCurve の既定の制御点（指定しなかった点はこの値のまま）。
    static let toneCurveDefaults: [String: (Double, Double)] = [
        "inputPoint0": (0, 0), "inputPoint1": (0.25, 0.25), "inputPoint2": (0.5, 0.5),
        "inputPoint3": (0.75, 0.75), "inputPoint4": (1, 1),
    ]

    static let parameters: [String: [String: PostFilterParameterKind]] = [
        "CIHighlightShadowAdjust": [
            "inputHighlightAmount": .number(0...1),
            "inputShadowAmount": .number(-1...1),
            "inputRadius": .number(0...10),
        ],
        "CIToneCurve": Dictionary(uniqueKeysWithValues: toneCurvePoints.map { ($0, PostFilterParameterKind.point(x: 0...1, y: 0...2)) }),
        "CIExposureAdjust": ["inputEV": .number(-5...5)],
        "CIColorClamp": ["inputMinComponents": .components4, "inputMaxComponents": .components4],
    ]
}

// MARK: - 解析

extension RequestParser {
    static let diagnosticsKeys: Set<String> = [
        "stats", "stats_color_space", "dump_float", "linear_space_filter", "post_filters", "skip_write",
    ]
    static let dumpFloatKeys: Set<String> = ["path", "color_space"]
    static let linearSpaceFilterKeys: Set<String> = ["kind", "value"]

    /// `diagnostics`（省略・null = 通常モード）。未知キー・型違い・値域外は INVALID_REQUEST。
    /// ファイルシステムには触れない（dump の親ディレクトリ等は PathValidator が見る）。
    static func parseDiagnostics(_ raw: Any?) throws -> Diagnostics? {
        guard let raw, !(raw is NSNull) else { return nil }
        guard let dict = raw as? [String: Any] else {
            throw DevelopError(.invalidRequest, "diagnostics must be a JSON object")
        }
        try rejectUnknownKeys(dict, allowed: diagnosticsKeys, context: "diagnostics")
        var diagnostics = Diagnostics()

        if let value = dict["stats"], !(value is NSNull) {
            guard let flag = boolean(value) else {
                throw DevelopError(.invalidRequest, "diagnostics.stats must be a boolean")
            }
            diagnostics.stats = flag
        }
        if let value = dict["stats_color_space"], !(value is NSNull) {
            diagnostics.statsColorSpace = try diagnosticsColorSpace(value, context: "diagnostics.stats_color_space")
        }
        if let value = dict["skip_write"], !(value is NSNull) {
            guard let flag = boolean(value) else {
                throw DevelopError(.invalidRequest, "diagnostics.skip_write must be a boolean")
            }
            diagnostics.skipWrite = flag
        }
        if let value = dict["dump_float"], !(value is NSNull) {
            diagnostics.dumpFloat = try parseDumpFloat(value)
        }
        if let value = dict["linear_space_filter"], !(value is NSNull) {
            diagnostics.linearSpaceFilter = try parseLinearSpaceFilter(value)
        }
        if let value = dict["post_filters"], !(value is NSNull) {
            diagnostics.postFilters = try parsePostFilters(value)
        }
        return diagnostics
    }

    static func diagnosticsColorSpace(_ value: Any, context: String) throws -> DiagnosticsColorSpace {
        guard let string = value as? String, let space = DiagnosticsColorSpace(rawValue: string) else {
            throw DevelopError(.invalidRequest, "\(context) must be \"linear_srgb\" or \"linear_p3\"")
        }
        return space
    }

    static func parseDumpFloat(_ raw: Any) throws -> DumpFloatRequest {
        guard let dict = raw as? [String: Any] else {
            throw DevelopError(.invalidRequest, "diagnostics.dump_float must be a JSON object")
        }
        try rejectUnknownKeys(dict, allowed: dumpFloatKeys, context: "diagnostics.dump_float")
        guard let path = dict["path"] as? String, !path.isEmpty else {
            throw DevelopError(.invalidRequest, "diagnostics.dump_float.path must be a non-empty string")
        }
        guard path.hasPrefix("/") else {
            throw DevelopError(.invalidRequest, "diagnostics.dump_float.path must be an absolute path")
        }
        guard !path.contains("\u{0}") else {
            throw DevelopError(.invalidRequest, "diagnostics.dump_float.path must not contain NUL")
        }
        let url = URL(fileURLWithPath: path).standardizedFileURL
        guard url.pathExtension.lowercased() == "f32" else {
            throw DevelopError(.invalidRequest, "diagnostics.dump_float.path must have the extension .f32")
        }
        var request = DumpFloatRequest(path: url)
        if let value = dict["color_space"], !(value is NSNull) {
            request.colorSpace = try diagnosticsColorSpace(value, context: "diagnostics.dump_float.color_space")
        }
        return request
    }

    static func parseLinearSpaceFilter(_ raw: Any) throws -> LinearSpaceFilterRequest {
        guard let dict = raw as? [String: Any] else {
            throw DevelopError(.invalidRequest, "diagnostics.linear_space_filter must be a JSON object")
        }
        try rejectUnknownKeys(dict, allowed: linearSpaceFilterKeys, context: "diagnostics.linear_space_filter")
        guard let string = dict["kind"] as? String, let kind = LinearSpaceFilterKind(rawValue: string) else {
            throw DevelopError(.invalidRequest, "diagnostics.linear_space_filter.kind must be \"identity\", \"scale\" or \"constant\"")
        }
        let rawValue = dict["value"].flatMap { $0 is NSNull ? nil : $0 }
        switch kind {
        case .identity:
            guard rawValue == nil else {
                throw DevelopError(.invalidRequest, "diagnostics.linear_space_filter.value is not allowed with kind \"identity\"")
            }
            return LinearSpaceFilterRequest(kind: .identity, value: nil)
        case .scale, .constant:
            let range = kind == .scale ? LinearSpaceFilterRequest.scaleRange : LinearSpaceFilterRequest.constantRange
            guard let rawValue, let value = number(rawValue), range.contains(value) else {
                throw DevelopError(
                    .invalidRequest,
                    "diagnostics.linear_space_filter.value must be a number in \(range.lowerBound)...\(range.upperBound) for kind \"\(kind.rawValue)\""
                )
            }
            return LinearSpaceFilterRequest(kind: kind, value: value)
        }
    }

    static func parsePostFilters(_ raw: Any) throws -> [PostFilterRequest] {
        guard let array = raw as? [Any] else {
            throw DevelopError(.invalidRequest, "diagnostics.post_filters must be an array")
        }
        guard array.count <= PostFilterAllowList.maxFilters else {
            throw DevelopError(.invalidRequest, "diagnostics.post_filters accepts at most \(PostFilterAllowList.maxFilters) filters")
        }
        return try array.enumerated().map { index, element in
            try parsePostFilter(element, context: "diagnostics.post_filters[\(index)]")
        }
    }

    static func parsePostFilter(_ raw: Any, context: String) throws -> PostFilterRequest {
        guard let dict = raw as? [String: Any] else {
            throw DevelopError(.invalidRequest, "\(context) must be a JSON object")
        }
        guard let name = dict["name"] as? String else {
            throw DevelopError(.invalidRequest, "\(context).name must be a string")
        }
        guard let allowed = PostFilterAllowList.parameters[name] else {
            throw DevelopError(
                .invalidRequest,
                "\(context).name must be one of \(PostFilterAllowList.names.joined(separator: ", "))"
            )
        }
        var keys = dict
        keys.removeValue(forKey: "name")
        try rejectUnknownKeys(keys, allowed: Set(allowed.keys), context: "\(context) (\(name)) parameter")

        var parameters: [String: FilterParameterValue] = [:]
        for (key, value) in keys {
            // null は「既定のまま」（他のキーと同じ扱い）。
            if value is NSNull { continue }
            let label = "\(context).\(key)"
            switch allowed[key]! {
            case .number(let range):
                guard let number = number(value), range.contains(number) else {
                    throw DevelopError(.invalidRequest, "\(label) must be a number in \(range.lowerBound)...\(range.upperBound)")
                }
                parameters[key] = .number(number)
            case .point(let xRange, let yRange):
                guard let values = numberArray(value, count: 2), xRange.contains(values[0]), yRange.contains(values[1]) else {
                    throw DevelopError(
                        .invalidRequest,
                        "\(label) must be [x, y] with x in \(xRange.lowerBound)...\(xRange.upperBound) and y in \(yRange.lowerBound)...\(yRange.upperBound)"
                    )
                }
                parameters[key] = .vector(values)
            case .components4:
                guard let values = numberArray(value, count: 4) else {
                    throw DevelopError(.invalidRequest, "\(label) must be [r, g, b, a] (4 finite numbers)")
                }
                parameters[key] = .vector(values)
            }
        }
        if name == "CIToneCurve" {
            try validateToneCurveMonotonic(parameters, context: context)
        }
        return PostFilterRequest(name: name, parameters: parameters)
    }

    /// CIToneCurve の制御点（指定しなかった点は既定値）の x が狭義単調増加か。
    static func validateToneCurveMonotonic(_ parameters: [String: FilterParameterValue], context: String) throws {
        var previous = -Double.infinity
        for key in PostFilterAllowList.toneCurvePoints {
            let x: Double
            if case .vector(let values)? = parameters[key] {
                x = values[0]
            } else {
                x = PostFilterAllowList.toneCurveDefaults[key]!.0
            }
            guard x > previous else {
                throw DevelopError(.invalidRequest, "\(context): CIToneCurve control point x must be strictly increasing (inputPoint0..4, unspecified points use the defaults)")
            }
            previous = x
        }
    }

    /// 有限の数値だけの配列（要素数固定）。bool は数値として受けない。
    static func numberArray(_ value: Any, count: Int) -> [Double]? {
        guard let array = value as? [Any], array.count == count else { return nil }
        let numbers = array.compactMap { number($0) }
        return numbers.count == count ? numbers : nil
    }
}

// MARK: - dump のパス検証（ファイルシステム）

extension PathValidator {
    /// dump_float.path の事前検証（読むだけ）。親ディレクトリが無ければ OUTPUT_DIR_NOT_FOUND、
    /// ディレクトリ・input / output と同一実体なら INVALID_REQUEST。
    static func validateDump(_ dump: URL, input: URL, output: URL, fileManager: FileManager) throws {
        let parent = dump.deletingLastPathComponent().path
        var parentIsDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: parent, isDirectory: &parentIsDirectory), parentIsDirectory.boolValue else {
            throw DevelopError(.outputDirNotFound, "diagnostics.dump_float.path directory does not exist: \(parent)")
        }
        var isDirectory: ObjCBool = false
        if fileManager.fileExists(atPath: dump.path, isDirectory: &isDirectory) {
            if isDirectory.boolValue {
                throw DevelopError(.invalidRequest, "diagnostics.dump_float.path is an existing directory: \(dump.path)")
            }
            if sameFile(input, dump, fileManager: fileManager) {
                throw DevelopError(.invalidRequest, "diagnostics.dump_float.path and input refer to the same file")
            }
            if sameFile(output, dump, fileManager: fileManager) {
                throw DevelopError(.invalidRequest, "diagnostics.dump_float.path and output refer to the same file")
            }
        }
        let resolved = dump.resolvingSymlinksInPath().path
        if resolved == input.resolvingSymlinksInPath().path {
            throw DevelopError(.invalidRequest, "diagnostics.dump_float.path and input refer to the same file")
        }
        if resolved == output.resolvingSymlinksInPath().path {
            throw DevelopError(.invalidRequest, "diagnostics.dump_float.path and output refer to the same file")
        }
    }
}

// MARK: - 応答

/// チャンネル 1 本の統計（全画面）。
public struct ChannelStats: Encodable, Equatable {
    public let mean: Double
    public let p50: Double
    public let p99: Double
    public let p999: Double
    public let max: Double
    public let min: Double
    /// 厳密に 1.0 超（extended 範囲に出た値）。
    public let frac_gt_1: Double
    /// `>= 1 − 1/65535`（16bit で白になる値）。
    public let frac_ge_1: Double
    public let frac_lt_0: Double
}

public struct ImageStats: Encodable, Equatable {
    public let width: Int
    public let height: Int
    public let r: ChannelStats
    public let g: ChannelStats
    public let b: ChannelStats
    /// いずれかのチャンネルが 1.0 超の画素の割合。
    public let any_gt_1: Double
    /// 3 チャンネルとも 1.0 超の画素の割合。
    public let all_gt_1: Double
}

public struct DumpFloatReport: Encodable {
    public let path: String
    public let width: Int
    public let height: Int
    public let channels = 3
    public let dtype = "float32"
    public let byte_order = "little"
    public let layout = "interleaved_rgb_row_major"
    public let color_space: String
    public let bytes: Int
}

public struct LinearSpaceFilterReport: Encodable {
    public let kind: String
    /// identity では null（キーは常に出す）。
    public let value: Double?
    public let filter = "CIColorMatrix"

    enum CodingKeys: String, CodingKey {
        case kind, value, filter
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(kind, forKey: .kind)
        try container.encode(value, forKey: .value)
        try container.encode(filter, forKey: .filter)
    }
}

public struct PostFilterReport: Encodable {
    public let name: String
    /// 許可リストのパラメータを filter から読み戻した値（指定しなかったものは CIFilter の既定値）。
    public let applied_parameters: [String: FilterParameterValue]
}

public struct DiagnosticsReadback: Encodable {
    public let gamut_mapping_enabled: Bool
    public let baseline_exposure: Double
    /// `outputImage` の extent [x, y, width, height]（1 回だけ参照した値。二重参照すると縮む: 設計書 §2.2）。
    public let extent: [Double]
}

/// 応答のトップレベル `diagnostics`（設計書 §4.4）。`diagnostics` 指定時だけ出す。
public struct DiagnosticsReport: Encodable {
    public let cli_diagnostics_version = 1
    public let written: Bool
    public let stats_color_space: String
    public let stats_source = "render_input"
    /// `diagnostics.stats: false` のときはキーごと出さない。
    public let stats: ImageStats?
    public let dump_float: DumpFloatReport?
    public let linear_space_filter: LinearSpaceFilterReport?
    public let post_filters: [PostFilterReport]?
    public let readback: DiagnosticsReadback
}

/// `--capabilities` の `diagnostics`。
public struct DiagnosticsCapability: Encodable {
    public let supported = true
    public let post_filters = PostFilterAllowList.names

    public init() {}
}

// MARK: - 実行

enum DiagnosticsPipeline {
    /// post_filters を順に掛ける。端は clampedToExtent で延長してから掛け、入力の extent に切り戻す
    /// （CIHighlightShadowAdjust の inputRadius 等で extent や縁の値が変わらないように）。
    static func applyPostFilters(_ image: CIImage, _ filters: [PostFilterRequest]) throws -> (CIImage, [PostFilterReport]) {
        let extent = image.extent
        var current = image.clampedToExtent()
        var reports: [PostFilterReport] = []
        for request in filters {
            guard let filter = CIFilter(name: request.name), let allowed = PostFilterAllowList.parameters[request.name] else {
                throw DevelopError(.renderFailed, "Core Image filter \(request.name) is not available")
            }
            filter.setDefaults()
            for (key, value) in request.parameters {
                switch value {
                case .number(let number):
                    filter.setValue(NSNumber(value: number), forKey: key)
                case .vector(let values):
                    filter.setValue(CIVector(values: values.map { CGFloat($0) }, count: values.count), forKey: key)
                }
            }
            filter.setValue(current, forKey: kCIInputImageKey)
            guard let output = filter.outputImage else {
                throw DevelopError(.renderFailed, "\(request.name) produced no output image")
            }
            current = output
            var applied: [String: FilterParameterValue] = [:]
            for key in allowed.keys {
                switch filter.value(forKey: key) {
                case let number as NSNumber:
                    applied[key] = .number(number.doubleValue)
                case let vector as CIVector:
                    applied[key] = .vector((0..<vector.count).map { Double(vector.value(at: $0)) })
                default:
                    break
                }
            }
            reports.append(PostFilterReport(name: request.name, applied_parameters: applied))
        }
        return (current.cropped(to: extent), reports)
    }

    /// RGBAf（clamp しない float）で描画する。
    static func renderFloatRGBA(_ image: CIImage, extent: CGRect, colorSpace: CGColorSpace, context: CIContext) throws -> [Float] {
        let width = Int(extent.width)
        let height = Int(extent.height)
        guard width > 0, height > 0 else {
            throw DevelopError(.renderFailed, "diagnostics: empty extent")
        }
        var buffer = [Float](repeating: 0, count: width * height * 4)
        buffer.withUnsafeMutableBytes { raw in
            context.render(image, toBitmap: raw.baseAddress!, rowBytes: width * 16, bounds: extent, format: .RGBAf, colorSpace: colorSpace)
        }
        return buffer
    }

    /// RGBA float → RGB float32 little-endian（行優先・アルファ無し）。
    static func packRGB(_ rgba: [Float], pixelCount: Int) -> Data {
        var data = Data(count: pixelCount * 3 * MemoryLayout<UInt32>.size)
        data.withUnsafeMutableBytes { raw in
            let out = raw.bindMemory(to: UInt32.self)
            rgba.withUnsafeBufferPointer { src in
                for i in 0..<pixelCount {
                    out[i * 3] = src[i * 4].bitPattern.littleEndian
                    out[i * 3 + 1] = src[i * 4 + 1].bitPattern.littleEndian
                    out[i * 3 + 2] = src[i * 4 + 2].bitPattern.littleEndian
                }
            }
        }
        return data
    }

    /// 統計・dump を取り、応答ブロックを組み立てる。描画は色空間ごとに 1 回（stats と dump が同じ色空間なら共有）。
    static func run(
        _ diagnostics: Diagnostics, image: CIImage, extent: CGRect, context: CIContext,
        written: Bool, filter: CIRAWFilter, decodedExtent: CGRect, postFilterReports: [PostFilterReport]?
    ) throws -> DiagnosticsReport {
        let width = Int(extent.width)
        let height = Int(extent.height)
        var rendered: [DiagnosticsColorSpace: [Float]] = [:]
        func buffer(_ space: DiagnosticsColorSpace) throws -> [Float] {
            if let cached = rendered[space] { return cached }
            let pixels = try renderFloatRGBA(image, extent: extent, colorSpace: space.cgColorSpace, context: context)
            rendered[space] = pixels
            return pixels
        }

        var stats: ImageStats?
        if diagnostics.stats {
            let pixels = try buffer(diagnostics.statsColorSpace)
            stats = pixels.withUnsafeBufferPointer { DiagnosticsStats.compute(rgba: $0, width: width, height: height) }
        }
        // stats と dump の色空間が違うときは、先の描画を捨ててからもう 1 回描く（full でバッファを 2 本抱えない）。
        if let dump = diagnostics.dumpFloat, dump.colorSpace != diagnostics.statsColorSpace {
            rendered.removeAll()
        }

        var dumpReport: DumpFloatReport?
        if let dump = diagnostics.dumpFloat {
            let pixels = try buffer(dump.colorSpace)
            let data = packRGB(pixels, pixelCount: width * height)
            try AtomicImageWriter.writeAtomically(data, to: dump.path)
            dumpReport = DumpFloatReport(
                path: dump.path.path, width: width, height: height, color_space: dump.colorSpace.rawValue, bytes: data.count
            )
        }

        let linearReport = diagnostics.linearSpaceFilter.map {
            LinearSpaceFilterReport(kind: $0.kind.rawValue, value: $0.value)
        }
        return DiagnosticsReport(
            written: written,
            stats_color_space: diagnostics.statsColorSpace.rawValue,
            stats: stats,
            dump_float: dumpReport,
            linear_space_filter: linearReport,
            post_filters: postFilterReports,
            readback: DiagnosticsReadback(
                gamut_mapping_enabled: filter.isGamutMappingEnabled,
                baseline_exposure: readable(filter.baselineExposure),
                extent: [decodedExtent.origin.x, decodedExtent.origin.y, decodedExtent.width, decodedExtent.height].map { Double($0) }
            )
        )
    }
}

// MARK: - 統計（純粋関数。単体テストは合成バッファで検証する）

public enum DiagnosticsStats {
    /// 16bit で白になる閾値（`frac_ge_1`）。
    public static let whiteThreshold16 = Float(1.0 - 1.0 / 65535.0)
    static let histogramBins = 65536

    /// percentile の順位（0 始まり）。事前実測の probe と同じ `min(n−1, Int(n·q))`（p50 は `n/2`）。
    public static func rank(_ q: Double, count n: Int) -> Int {
        Swift.min(n - 1, Int(Double(n) * q))
    }

    /// RGBA（float、4 成分/画素）のバッファから全画面のチャンネル別統計を出す。
    /// 非有限値は mean / min / max / percentile から除き、割合の分母は全画素数のまま。
    public static func compute(rgba: UnsafeBufferPointer<Float>, width: Int, height: Int) -> ImageStats {
        let n = width * height
        precondition(rgba.count >= n * 4, "buffer is smaller than width × height × 4")
        let channels = (0..<3).map { channel(rgba, offset: $0, pixelCount: n) }
        var any = 0
        var all = 0
        for i in 0..<n {
            let r = rgba[i * 4] > 1
            let g = rgba[i * 4 + 1] > 1
            let b = rgba[i * 4 + 2] > 1
            if r || g || b { any += 1 }
            if r && g && b { all += 1 }
        }
        let denominator = Double(Swift.max(n, 1))
        return ImageStats(
            width: width, height: height, r: channels[0], g: channels[1], b: channels[2],
            any_gt_1: Double(any) / denominator, all_gt_1: Double(all) / denominator
        )
    }

    static func channel(_ rgba: UnsafeBufferPointer<Float>, offset: Int, pixelCount n: Int) -> ChannelStats {
        var finite = 0
        var sum = 0.0
        var lo = Float.infinity
        var hi = -Float.infinity
        var gt1 = 0
        var ge1 = 0
        var lt0 = 0
        for i in 0..<n {
            let v = rgba[i * 4 + offset]
            guard v.isFinite else { continue }
            finite += 1
            sum += Double(v)
            if v < lo { lo = v }
            if v > hi { hi = v }
            if v > 1 { gt1 += 1 }
            if v >= whiteThreshold16 { ge1 += 1 }
            if v < 0 { lt0 += 1 }
        }
        let denominator = Double(Swift.max(n, 1))
        guard finite > 0 else {
            return ChannelStats(mean: 0, p50: 0, p99: 0, p999: 0, max: 0, min: 0, frac_gt_1: 0, frac_ge_1: 0, frac_lt_0: 0)
        }
        let quantiles = [0.5, 0.99, 0.999]
        let values = exactPercentiles(rgba, offset: offset, pixelCount: n, finiteCount: finite, lo: lo, hi: hi,
                                      ranks: quantiles.map { rank($0, count: finite) })
        return ChannelStats(
            mean: sum / Double(finite),
            p50: readable(values[0]), p99: readable(values[1]), p999: readable(values[2]),
            max: readable(hi), min: readable(lo),
            frac_gt_1: Double(gt1) / denominator, frac_ge_1: Double(ge1) / denominator, frac_lt_0: Double(lt0) / denominator
        )
    }

    /// 厳密な順位統計（近似しない）。[lo, hi] の 65536 ビンのヒストグラムで目標順位の入るビンを探し、
    /// そのビンの値だけを集めてソートする（全体をソートしない。full 24M 画素でも O(n)）。
    static func exactPercentiles(
        _ rgba: UnsafeBufferPointer<Float>, offset: Int, pixelCount n: Int, finiteCount: Int, lo: Float, hi: Float, ranks: [Int]
    ) -> [Float] {
        guard hi > lo else { return ranks.map { _ in lo } }
        let bins = histogramBins
        let scale = Double(bins) / (Double(hi) - Double(lo))
        @inline(__always) func bin(_ v: Float) -> Int {
            Swift.min(bins - 1, Swift.max(0, Int((Double(v) - Double(lo)) * scale)))
        }
        var counts = [Int](repeating: 0, count: bins)
        for i in 0..<n {
            let v = rgba[i * 4 + offset]
            guard v.isFinite else { continue }
            counts[bin(v)] += 1
        }
        // 各順位について (ビン, ビン内の順位)。
        var targets: [(bin: Int, offset: Int)] = []
        for rank in ranks {
            var cumulative = 0
            var found = (bin: bins - 1, offset: 0)
            for b in 0..<bins {
                if cumulative + counts[b] > rank {
                    found = (b, rank - cumulative)
                    break
                }
                cumulative += counts[b]
            }
            targets.append(found)
        }
        // ビン → members の添字（対象外は -1）。画素ごとの Set 参照を避ける。
        var slot = [Int](repeating: -1, count: bins)
        var members: [[Float]] = []
        for target in targets where slot[target.bin] < 0 {
            slot[target.bin] = members.count
            var list: [Float] = []
            list.reserveCapacity(counts[target.bin])
            members.append(list)
        }
        for i in 0..<n {
            let v = rgba[i * 4 + offset]
            guard v.isFinite else { continue }
            let s = slot[bin(v)]
            if s >= 0 { members[s].append(v) }
        }
        for s in members.indices { members[s].sort() }
        return targets.map { members[slot[$0.bin]][$0.offset] }
    }
}
