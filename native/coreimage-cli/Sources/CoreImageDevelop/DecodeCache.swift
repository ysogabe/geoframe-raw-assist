import CoreImage
import Foundation

// `--batch` の decode キャッシュ op（CLI 0.7.0、Issue #116。設計 docs/reports/coreimage-small-render-performance.md §5.3）。
//
// - `op: decode`  デコード段（RAW フィルタ・レンズ補正・デコーダ設定）だけを実行し、結果の RGBAh ビットマップを
//                  プロセス内に保持してハンドルを返す。
// - `op: render`  ハンドルと adjustments から後段チェーンだけを掛けて書き出す。
// - `op: release` ハンドルを解放する。
//
// 保持するのは `CIImage` でも `CIRAWFilter` でもなく、half float RGBA のビットマップ（`Data`）。
// `CIRAWFilter.outputImage` を 2 回参照すると extent が縮む問題（設計書 §2.2）に触れず、`CIContext.clearCaches()` でも
// 消えない（CI のキャッシュとは別物。README「decode キャッシュ」）。

// MARK: - キャッシュ本体

/// デコード段の結果 1 件。後段の入力になる画素と、応答を組み立てるための記録を持つ。
final class DecodedEntry {
    let id: String
    let input: URL
    let mode: Mode
    /// デコード段で決まる値（render の adjustments がこれと食い違えば INVALID_REQUEST）。
    let stage: DecodeStageKey
    /// 画素（RGBAh、作業空間 extended linear sRGB のまま。行バイト数 = `extent.width * 8`）。
    let bitmap: Data
    /// 保持した画像の extent（`CIRAWFilter.outputImage.extent.integral`）。
    let extent: CGRect
    let iso: Int?
    let plan: AdjustmentsDecoderPlan
    let guards: [String]
    /// decode 時に filter から読み戻した値（`format` / `color_space` は render 時に差し替える）。
    let applied: Applied
    let source: SourceInfo
    let profile: Profile

    init(
        id: String, input: URL, mode: Mode, stage: DecodeStageKey, bitmap: Data, extent: CGRect, iso: Int?,
        plan: AdjustmentsDecoderPlan, guards: [String], applied: Applied, source: SourceInfo, profile: Profile
    ) {
        self.id = id
        self.input = input
        self.mode = mode
        self.stage = stage
        self.bitmap = bitmap
        self.extent = extent
        self.iso = iso
        self.plan = plan
        self.guards = guards
        self.applied = applied
        self.source = source
        self.profile = profile
    }

    var byteCount: Int { bitmap.count }

    /// 保持した画素から `CIImage` を作る（毎回作る。ビットマップはコピーされない）。
    func makeImage() -> CIImage {
        let image = CIImage(
            bitmapData: bitmap, bytesPerRow: Int(extent.width) * 8, size: extent.size, format: .RGBAh, colorSpace: nil
        )
        if extent.origin == .zero { return image }
        return image.transformed(by: CGAffineTransform(translationX: extent.origin.x, y: extent.origin.y))
    }
}

/// デコード段のキャッシュキーのうち、`adjustments` から来る部分（hr の寄与を合成した実効値）。
/// 入力・mode・max_edge・decoder_version・settings は decode 時に固定される（render は指定できない）。
struct DecodeStageKey: Equatable {
    var exposure: Double
    var wbTemp: Double
    /// デコーダ内シャープに効くのは `min(sharpness, 1)`（> 1 は後段 USM の増分）。
    var decoderSharpness: Double
    var noiseReduction: NoiseReduction

    init(_ resolved: Adjustments) {
        exposure = resolved.exposure
        wbTemp = resolved.wbTemp
        decoderSharpness = min(resolved.sharpness, 1.0)
        noiseReduction = resolved.noiseReduction
    }

    /// 食い違うキーの説明（一致なら空）。
    func mismatches(with other: DecodeStageKey) -> [String] {
        var result: [String] = []
        func differs(_ a: Double, _ b: Double) -> Bool { abs(a - b) > 1e-9 }
        if differs(exposure, other.exposure) {
            result.append("exposure (+highlight_recovery) \(other.exposure) vs decoded \(exposure)")
        }
        if differs(wbTemp, other.wbTemp) { result.append("wb_temp \(other.wbTemp) vs decoded \(wbTemp)") }
        if differs(decoderSharpness, other.decoderSharpness) {
            result.append("min(sharpness, 1) \(other.decoderSharpness) vs decoded \(decoderSharpness)")
        }
        if noiseReduction != other.noiseReduction {
            result.append("noise_reduction \(other.noiseReduction.rawValue) vs decoded \(noiseReduction.rawValue)")
        }
        return result
    }
}

/// 保持の上限。個数かバイト数のどちらかを超えたら、最近使っていないものから捨てる。
public struct DecodeCacheLimits: Equatable {
    /// 同時に保持するハンドルの数（既定 32）。
    public var maxEntries: Int
    /// 保持する画素の合計バイト数（既定 256 MiB）。長辺 640 は 1 件 約 2.2 MB、2400 は約 31 MB。
    public var maxBytes: Int

    public init(maxEntries: Int = 32, maxBytes: Int = 256 * 1024 * 1024) {
        self.maxEntries = maxEntries
        self.maxBytes = maxBytes
    }
}

/// LRU のデコード結果キャッシュ（`BatchProcessor` が 1 つ持つ。スレッド安全性は不要: バッチは 1 行ずつ直列）。
final class DecodeCache {
    let limits: DecodeCacheLimits
    /// 先頭が最も古い（LRU）、末尾が最も新しい。
    private var order: [String] = []
    private var entries: [String: DecodedEntry] = [:]
    private(set) var bytes = 0
    /// 捨てた（LRU）ハンドルの履歴。HANDLE_NOT_FOUND の文言を「捨てられた」と「未知」で変えるためだけに持つ。
    private var evictedHistory: [String] = []
    private static let historyLimit = 256

    init(limits: DecodeCacheLimits) {
        self.limits = limits
    }

    var count: Int { entries.count }

    /// 追加して、上限を超えた分を LRU で捨てる。捨てたハンドルを返す（新規の分は捨てない）。
    func insert(_ entry: DecodedEntry) -> [String] {
        entries[entry.id] = entry
        order.append(entry.id)
        bytes += entry.byteCount
        var evicted: [String] = []
        while (entries.count > limits.maxEntries || bytes > limits.maxBytes), let oldest = order.first, oldest != entry.id {
            order.removeFirst()
            if let removed = entries.removeValue(forKey: oldest) { bytes -= removed.byteCount }
            evicted.append(oldest)
            evictedHistory.append(oldest)
        }
        if evictedHistory.count > Self.historyLimit { evictedHistory.removeFirst(evictedHistory.count - Self.historyLimit) }
        return evicted
    }

    /// 取り出して最新扱いにする。
    func touch(_ id: String) -> DecodedEntry? {
        guard let entry = entries[id] else { return nil }
        if let index = order.firstIndex(of: id) { order.remove(at: index) }
        order.append(id)
        return entry
    }

    @discardableResult
    func remove(_ id: String) -> Bool {
        guard let entry = entries.removeValue(forKey: id) else { return false }
        bytes -= entry.byteCount
        if let index = order.firstIndex(of: id) { order.remove(at: index) }
        return true
    }

    func wasEvicted(_ id: String) -> Bool { evictedHistory.contains(id) }

    /// 保持している順（古い→新しい）。テスト用。
    var handles: [String] { order }
}

// MARK: - リクエスト

struct DecodeOpRequest {
    var input: URL
    var mode: Mode
    var maxEdge: Int?
    var decoderVersion: String?
    var settings: Settings
    var adjustments: Adjustments
}

struct RenderOpRequest {
    var handle: String
    var output: URL
    var format: OutputFormat
    var jpegQuality: Double?
    var outputColorSpace: OutputColorSpace?
    var adjustments: Adjustments
}

extension RequestParser {
    static let batchOps: [String] = ["decode", "render", "release"]
    static let decodeOpKeys: Set<String> = ["op", "input", "mode", "max_edge", "decoder_version", "settings", "adjustments"]
    static let renderOpKeys: Set<String> = ["op", "handle", "output", "format", "jpeg_quality", "output_color_space", "adjustments"]
    static let releaseOpKeys: Set<String> = ["op", "handle"]

    /// `--batch` の 1 行が op 付きか。キー `op` が無ければ nil（従来の現像リクエスト）。
    /// JSON として読めない行・オブジェクトでない行も nil（従来の経路が同じエラーを返す）。
    static func operation(in data: Data) throws -> (op: String, object: [String: Any])? {
        guard let object = (try? JSONSerialization.jsonObject(with: data, options: [])) as? [String: Any],
              let raw = object["op"]
        else { return nil }
        guard let op = raw as? String, batchOps.contains(op) else {
            throw DevelopError(.invalidRequest, "op must be one of \(batchOps.map { "\"\($0)\"" }.joined(separator: ", ")) (or omitted for a develop request)")
        }
        return (op, object)
    }

    static func parseDecodeOp(_ dict: [String: Any]) throws -> DecodeOpRequest {
        try rejectUnknownKeys(dict, allowed: decodeOpKeys, context: "decode request")
        let input = try absolutePath(dict, "input")
        guard input.pathExtension.lowercased() == "arw" else {
            throw DevelopError(.invalidRequest, "input must be a Sony .ARW file")
        }
        let (mode, maxEdge) = try parseModeAndMaxEdge(dict)
        let decoderVersion = try parseDecoderVersion(dict)
        let settings = try parseSettings(dict["settings"])
        guard let adjustments = try parseAdjustments(dict["adjustments"]) else {
            throw DevelopError(.invalidRequest, "decode requires adjustments (the decode stage values come from it; post-stage keys are ignored)")
        }
        try validateAdjustmentsAgainstSettings(dict["settings"], settings)
        return DecodeOpRequest(
            input: input, mode: mode, maxEdge: maxEdge, decoderVersion: decoderVersion, settings: settings, adjustments: adjustments
        )
    }

    static func parseHandle(_ dict: [String: Any]) throws -> String {
        guard let handle = dict["handle"] as? String, !handle.isEmpty, handle.count <= 128 else {
            throw DevelopError(.invalidRequest, "handle must be a non-empty string")
        }
        return handle
    }

    static func parseRenderOp(_ dict: [String: Any]) throws -> RenderOpRequest {
        try rejectUnknownKeys(dict, allowed: renderOpKeys, context: "render request")
        let handle = try parseHandle(dict)
        let output = try absolutePath(dict, "output")
        let (format, jpegQuality, outputColorSpace) = try parseOutputSpec(dict, output: output)
        guard let adjustments = try parseAdjustments(dict["adjustments"]) else {
            throw DevelopError(.invalidRequest, "render requires adjustments")
        }
        return RenderOpRequest(
            handle: handle, output: output, format: format, jpegQuality: jpegQuality,
            outputColorSpace: outputColorSpace, adjustments: adjustments
        )
    }

    static func parseReleaseOp(_ dict: [String: Any]) throws -> String {
        try rejectUnknownKeys(dict, allowed: releaseOpKeys, context: "release request")
        return try parseHandle(dict)
    }
}

// MARK: - 応答

public struct CacheState: Encodable {
    public let entries: Int
    public let bytes: Int
    public let max_entries: Int
    public let max_bytes: Int
    /// この op で LRU により捨てたハンドル（decode のときだけ空でないことがある）。
    public let evicted: [String]
}

public struct DecodeResponse: Encodable {
    public let ok = true
    public let op = "decode"
    public let engine = "coreimage"
    public let cli_version = cliVersion
    public let macos_version = macOSVersionString
    public let handle: String
    public let decoder_version: String
    public let input: String
    public let mode: String
    public let width: Int
    public let height: Int
    /// 保持した画素のバイト数。
    public let bytes: Int
    /// `decode`: フィルタの生成〜設定、`render_encode`: 実デコード + ビットマップへの書き出し。
    public let elapsed_ms: ElapsedMS
    public let applied: Applied
    public let source: SourceInfo
    public let cache: CacheState
    public var sequence: Int? = nil
}

public struct ReleaseResponse: Encodable {
    public let ok = true
    public let op = "release"
    public let handle: String
    /// 解放した（保持していた）なら true。未知・解放済み・捨て済みなら false（エラーにはしない）。
    public let released: Bool
    public let cache: CacheState
    public var sequence: Int? = nil
}

// MARK: - 実行

/// 一度に作れるハンドル ID の接頭辞（プロセスごとに違う。別プロセスの古いハンドルを取り違えない）。
func makeHandlePrefix() -> String {
    "h" + String(UUID().uuidString.prefix(8)).lowercased()
}

extension DecodeCache {
    func state(evicted: [String] = []) -> CacheState {
        CacheState(entries: count, bytes: bytes, max_entries: limits.maxEntries, max_bytes: limits.maxBytes, evicted: evicted)
    }
}

/// `op: decode`。デコード段を実行して RGBAh ビットマップを保持し、ハンドルを返す。
func performDecode(_ request: DecodeOpRequest, handle: String, cache: DecodeCache, context: CIContext) throws -> DecodeResponse {
    try PathValidator.validateInput(request.input)
    let t0 = DispatchTime.now()
    let configured = try configureRAWFilter(
        input: request.input, maxEdge: request.maxEdge, decoderVersion: request.decoderVersion,
        settings: request.settings, adjustments: request.adjustments
    )
    guard let resolved = configured.resolved, let plan = configured.plan else {
        throw DevelopError(.internalError, "decode stage plan is missing")
    }
    let filter = configured.filter
    // outputImage の参照は 1 回だけ（設計書 §2.2）。
    guard let decoded = filter.outputImage else {
        throw DevelopError(.renderFailed, "CIRAWFilter produced no output image")
    }
    guard isUsableExtent(decoded.extent) else {
        throw DevelopError(.renderFailed, "decoded image has an invalid extent")
    }
    let extent = decoded.extent.integral
    let width = Int(extent.width)
    let height = Int(extent.height)
    let rowBytes = width * 8
    let byteCount = rowBytes * height
    guard byteCount <= cache.limits.maxBytes else {
        throw DevelopError(.invalidRequest, "decoded image (\(byteCount) bytes) exceeds the decode cache limit (\(cache.limits.maxBytes) bytes); use a smaller max_edge")
    }
    let t1 = DispatchTime.now()

    var bitmap = Data(count: byteCount)
    bitmap.withUnsafeMutableBytes { buffer in
        // colorSpace nil: 作業空間の値をそのまま書く（色変換を挟まない）。読み戻す側（makeImage）も nil。
        context.render(decoded, toBitmap: buffer.baseAddress!, rowBytes: rowBytes, bounds: extent, format: .RGBAh, colorSpace: nil)
    }
    let t2 = DispatchTime.now()

    let applied = makeApplied(filter, profile: request.settings.profile, format: .tiff16, colorSpaceName: "(set by render)")
    let source = makeSourceInfo(filter, native: configured.native)
    let entry = DecodedEntry(
        id: handle, input: request.input, mode: request.mode, stage: DecodeStageKey(resolved.adjustments), bitmap: bitmap,
        extent: extent, iso: isoSpeed(from: filter.properties), plan: plan, guards: configured.guards,
        applied: applied, source: source, profile: request.settings.profile
    )
    let evicted = cache.insert(entry)
    return DecodeResponse(
        handle: handle, decoder_version: filter.decoderVersion.rawValue, input: request.input.path,
        mode: request.mode.rawValue, width: width, height: height, bytes: byteCount,
        elapsed_ms: ElapsedMS(decode: millis(t0, t1), render_encode: millis(t1, t2), total: millis(t0, t2)),
        applied: applied, source: source, cache: cache.state(evicted: evicted)
    )
}

/// `op: render`。保持した画素に後段チェーンだけを掛けて書き出す。応答は通常の現像と同じ形（+ `op` / `handle`）。
/// `elapsed_ms.decode` は後段グラフの組み立て、`render_encode` はレンダ + エンコード + 書き込み。
func performRender(_ request: RenderOpRequest, cache: DecodeCache, context: CIContext) throws -> SuccessResponse {
    guard let entry = cache.touch(request.handle) else {
        let why = cache.wasEvicted(request.handle) ? "was evicted (LRU limit)" : "is unknown or already released"
        throw DevelopError(.handleNotFound, "handle \(request.handle) \(why); decode again")
    }
    try PathValidator.validateOutput(request.output, input: entry.input)
    if request.output.standardizedFileURL.path == entry.input.standardizedFileURL.path {
        throw DevelopError(.invalidRequest, "input and output must be different paths")
    }
    let t0 = DispatchTime.now()
    let (resolvedAdjustments, effect) = HighlightRecoveryMapping.resolve(request.adjustments)
    let mismatches = entry.stage.mismatches(with: DecodeStageKey(resolvedAdjustments))
    guard mismatches.isEmpty else {
        throw DevelopError(.invalidRequest, "adjustments differ from the decoded stage (decode again): \(mismatches.joined(separator: "; "))")
    }

    let (image, adjustmentsApplied) = try applyAdjustmentsPost(
        to: entry.makeImage(), decodedExtent: entry.extent, iso: entry.iso,
        adjustments: resolvedAdjustments, effect: effect, plan: entry.plan, guards: entry.guards
    )
    guard isUsableExtent(image.extent) else {
        throw DevelopError(.renderFailed, "output image has an invalid extent")
    }
    let extent = image.extent.integral
    let t1 = DispatchTime.now()

    let (colorSpace, colorSpaceName) = OutputColor.space(for: request.format, colorSpace: request.outputColorSpace)
    let cgImage: CGImage
    switch request.format {
    case .jpeg:
        guard let rendered = context.createCGImage(image, from: extent, format: .RGBA8, colorSpace: colorSpace) else {
            throw DevelopError(.renderFailed, "Core Image failed to render the image")
        }
        cgImage = rendered
    case .tiff16:
        cgImage = try renderRGB16(image, extent: extent, colorSpace: colorSpace, context: context)
    }
    try AtomicImageWriter.write(cgImage, to: request.output, format: request.format, jpegQuality: request.jpegQuality)
    let t2 = DispatchTime.now()

    var applied = entry.applied
    applied.color_space = colorSpaceName
    applied.bit_depth = request.format == .jpeg ? 8 : 16
    var response = SuccessResponse(
        cli_version: cliVersion,
        macos_version: macOSVersionString,
        decoder_version: applied.decoder_version,
        input: entry.input.path,
        output: request.output.path,
        mode: entry.mode.rawValue,
        format: request.format.rawValue,
        width: cgImage.width,
        height: cgImage.height,
        elapsed_ms: ElapsedMS(decode: millis(t0, t1), render_encode: millis(t1, t2), total: millis(t0, t2)),
        applied: applied,
        source: entry.source,
        adjustments_applied: adjustmentsApplied,
        diagnostics: nil
    )
    response.op = "render"
    response.handle = entry.id
    return response
}
