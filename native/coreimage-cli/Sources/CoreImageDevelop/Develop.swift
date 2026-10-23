import CoreImage
import Foundation
import ImageIO
import UniformTypeIdentifiers

// MARK: - 応答

public struct ElapsedMS: Encodable {
    /// CIRAWFilter の生成・パラメータ設定・outputImage の取得まで。Core Image は遅延評価なので
    /// 実際のデモザイク等の RAW デコードはここに含まれず、`render_encode` 側に入る。
    public let decode: Double
    /// CIContext でのレンダリング（実際の RAW デコードを含む）＋エンコード＋rename。
    public let render_encode: Double
    public let total: Double
}

/// CIRAWFilter に設定した後、filter から読み戻した値。
public struct Applied: Encodable {
    public let profile: String
    public let decoder_version: String
    public let scale_factor: Double
    public let exposure: Double
    public let baseline_exposure: Double
    public let boost_amount: Double
    public let boost_shadow_amount: Double
    public let shadow_bias: Double
    public let neutral_temperature: Double
    public let neutral_tint: Double
    public let sharpness_amount: Double
    public let contrast_amount: Double
    public let detail_amount: Double
    public let luminance_noise_reduction_amount: Double
    public let color_noise_reduction_amount: Double
    public let moire_reduction_amount: Double
    public let local_tone_map_amount: Double
    public let extended_dynamic_range_amount: Double
    public let gamut_mapping_enabled: Bool
    public let draft_mode_enabled: Bool
    public let lens_correction_enabled: Bool
    public let lens_correction_supported: Bool
    public let local_tone_map_supported: Bool
    public let orientation: UInt32
    public let working_color_space: String
    public var color_space: String
    public var bit_depth: Int
    public let orientation_handling: String
}

public struct SourceInfo: Encodable {
    public let camera_model: String?
    public let lens_model: String?
    public let iso: Int?
    public let native_width: Int
    public let native_height: Int
    public let orientation: UInt32
}

public struct SuccessResponse: Encodable {
    public let ok = true
    public let engine = "coreimage"
    public let cli_version: String
    public let macos_version: String
    public let decoder_version: String
    public let input: String
    public let output: String
    public let mode: String
    public let format: String
    public let width: Int
    public let height: Int
    public let elapsed_ms: ElapsedMS
    public let applied: Applied
    public let source: SourceInfo
    /// `adjustments` 指定時だけ値を持つ（Issue #73）。nil のときはキー自体を出さない（synthesized Encodable は
    /// Optional を encodeIfPresent で書く）。既定リクエストの応答は 0.2.0 と同じキー集合のまま。
    public var adjustments_applied: AdjustmentsApplied? = nil
    /// `diagnostics` 指定時だけ値を持つ（CLI 0.4.0、Issue #92 設計書 §4.4）。nil のときはキー自体を出さない。
    public var diagnostics: DiagnosticsReport? = nil
    /// `--batch` のときだけ値を持つ（CLI 0.5.0、Issue #82）。受け付けた順の番号（1 始まり）。
    /// 単発呼び出しでは nil のままで、キー自体を出さない。
    public var sequence: Int? = nil
    /// `--batch` の `op: render` のときだけ値を持つ（CLI 0.7.0、Issue #116）。通常の応答にはキー自体を出さない。
    public var op: String? = nil
    public var handle: String? = nil
}

/// `adjustments` をどう解釈したか（応答のトップレベル `adjustments_applied`。設計書 §4.2）。
/// `applied` は変えない（最終的に filter から読み戻した値はそちらに載る）。
public struct AdjustmentsApplied: Encodable {
    /// profile 適用直後・adjustments 適用前に filter から読み戻した値（= apple_default の既定値）。
    public struct Defaults: Encodable {
        public let sharpness_amount: Double
        public let luminance_noise_reduction_amount: Double
        public let color_noise_reduction_amount: Double
        public let neutral_temperature: Double
        public let neutral_tint: Double
    }

    public struct USM: Encodable {
        public let sigma: Double
        public let gain: Double
        public let coring: Double
        public let long_edge: Int
        /// 撮影 ISO。不明なら null（キーは常に出す）。
        public let iso: Int?

        enum CodingKeys: String, CodingKey {
            case sigma, gain, coring, long_edge, iso
        }

        public func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(sigma, forKey: .sigma)
            try container.encode(gain, forKey: .gain)
            try container.encode(coring, forKey: .coring)
            try container.encode(long_edge, forKey: .long_edge)
            try container.encode(iso, forKey: .iso)
        }
    }

    public struct WB: Encodable {
        /// 基準の色温度（profile 適用後の読み戻し = 撮影時 WB）。
        public let k0: Double
        /// 実際に掛けた mired の変化量（1e6/K − 1e6/K0。wb_temp > 0 で負 = 暖色側）。
        public let mired_shift: Double
        public let mired_per_unit: Double
    }

    /// 後段に実際に使った値。
    public struct Post: Encodable {
        public let shadow_lift: Double
        public let brightness: Double
        public let contrast: Double
        public let saturation: Double
        public let usm: USM
        public let wb: WB
        public let noise_reduction: String
    }

    /// `highlight_recovery` の写像と合成の結果（CLI 0.6.0、Issue #81）。
    /// `effective_exposure` はデコード段（`applied.exposure`）、`effective_shadow_lift`（0..100）÷ 100 が `post.shadow_lift`。
    public struct HighlightRecovery: Encodable {
        public let value: Double
        public let exposure_delta: Double
        public let shadow_lift_delta: Double
        public let effective_exposure: Double
        public let effective_shadow_lift: Double
        public let shadow_lift_clamped: Bool

        init(_ effect: HighlightRecoveryEffect) {
            value = effect.value
            exposure_delta = effect.exposureDelta
            shadow_lift_delta = effect.shadowLiftDelta
            effective_exposure = effect.effectiveExposure
            effective_shadow_lift = effect.effectiveShadowLift
            shadow_lift_clamped = effect.shadowLiftClamped
        }
    }

    public let defaults: Defaults
    public let post: Post
    public let highlight_recovery: HighlightRecovery
    /// 受理したが効かせなかったキー。0.6.0（#81）で空配列になった（フィールドは契約として残す）。
    public let ignored: [String]
    /// 発動したガード（例: `white_dot_sharpness_floor`）。
    public let guards: [String]
}

public struct ErrorBody: Encodable {
    public let code: ErrorCode
    public let message: String
}

public struct FailureResponse: Encodable {
    public let ok = false
    public let error: ErrorBody
    /// `--batch` のときだけ値を持つ（`SuccessResponse.sequence` と同じ）。単発では出さない。
    public var sequence: Int? = nil

    public init(_ error: DevelopError, sequence: Int? = nil) {
        self.error = ErrorBody(code: error.code, message: error.message)
        self.sequence = sequence
    }
}

public func encodeJSONLine<T: Encodable>(_ value: T) -> String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    // filter から読み戻した値が非有限（壊れたメタデータ等）でも応答を落とさない（JSON に数値としては書けないので
    // 文字列 "nan" / "inf" にする。通常の応答には現れない。`adjustments_applied.guards` の記録を届けるため）。
    encoder.nonConformingFloatEncodingStrategy = .convertToString(positiveInfinity: "inf", negativeInfinity: "-inf", nan: "nan")
    guard let data = try? encoder.encode(value), let string = String(data: data, encoding: .utf8) else {
        return #"{"error":{"code":"INTERNAL_ERROR","message":"failed to encode response"},"ok":false}"#
    }
    return string
}

public var macOSVersionString: String {
    let v = ProcessInfo.processInfo.operatingSystemVersion
    return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
}

// MARK: - 現像

/// α6400 の ARW として扱うカメラ名（CIRAWFilter.supportedCameraModels 上の表記）。
public let ilce6400ModelName = "Sony Alpha ILCE-6400"

/// 出力色空間の表記（応答 `applied.color_space` に載せる）。
enum OutputColor {
    static func space(for format: OutputFormat, colorSpace: OutputColorSpace?) -> (CGColorSpace, String) {
        switch (format, colorSpace ?? .displayP3) {
        case (.jpeg, _), (.tiff16, .srgb):
            return (CGColorSpace(name: CGColorSpace.sRGB)!, "sRGB IEC61966-2.1")
        case (.tiff16, .displayP3):
            return (CGColorSpace(name: CGColorSpace.displayP3)!, "Display P3")
        }
    }
}

/// Float を JSON 上で短く読める Double へ（0.68f → 0.68。0.6800000071525879 にしない）。
func readable(_ value: Float) -> Double {
    Double("\(value)") ?? Double(value)
}

func millis(_ start: DispatchTime, _ end: DispatchTime) -> Double {
    let ns = Double(end.uptimeNanoseconds - start.uptimeNanoseconds)
    return (ns / 1_000_000 * 10).rounded() / 10
}

/// 段階別の計時（Issue #102）。環境変数 `GEOFRAME_CI_TIMING=1` のときだけ、1 件ごとに stderr へ 1 行書く。
/// stdout の応答 JSON・出力画像には一切影響しない（時刻を取るだけ）。Core Image は遅延評価なので、
/// RAW のデコード・デモザイク・レンズ補正・後段カーネルはすべて `render`（CIContext のレンダ）に入る。
/// 段階をさらに分けた計測は docs/reports/coreimage-small-render-performance.md と scripts/coreimage-small-decode/。
public enum StageTiming {
    public static let environmentKey = "GEOFRAME_CI_TIMING"
    public static let enabled = ProcessInfo.processInfo.environment[environmentKey] == "1"

    /// `timing create=… configure=… graph=… render=… encode_write=… other=… total=…`（ms）。
    /// - create: `CIRAWFilter(imageURL:)`（ファイルを開きメタデータを読む）
    /// - configure: decoderVersion〜adjustments のデコード段・上書き・白点ガード
    /// - graph: `outputImage` 取得と後段チェーンのグラフ組み立て（画素は計算しない）
    /// - render: CIContext のレンダ（RAW デコード〜後段カーネル〜読み出し）。skip_write では -1
    /// - encode_write: エンコード・一時ファイル書き込み・rename。skip_write では -1
    /// - other: 上記以外（単発呼び出しでの CIContext の生成、診断モードの統計など）
    static func line(
        t0: DispatchTime, created: DispatchTime, configured: DispatchTime, graphBuilt: DispatchTime,
        renderStart: DispatchTime?, rendered: DispatchTime?, written: DispatchTime?, end: DispatchTime
    ) -> String {
        let render = renderStart.flatMap { start in rendered.map { millis(start, $0) } } ?? -1
        let encode = rendered.flatMap { start in written.map { millis(start, $0) } } ?? -1
        let accounted = millis(t0, graphBuilt) + max(0, render) + max(0, encode)
        let other = ((millis(t0, end) - accounted) * 10).rounded() / 10
        return "timing create=\(millis(t0, created)) configure=\(millis(created, configured)) graph=\(millis(configured, graphBuilt))"
            + " render=\(render) encode_write=\(encode) other=\(other) total=\(millis(t0, end))"
    }

    static func report(_ line: String) {
        FileHandle.standardError.write(Data(("geoframe-ci-develop: " + line + "\n").utf8))
    }
}

/// 現像に使う CIContext（作業空間 extended linear sRGB・half float、中間結果はキャッシュしない）。
/// 単発呼び出しでは 1 件ごとに作り、`--batch` ではプロセスで 1 つを使い回す。
public func makeRenderContext() -> CIContext {
    CIContext(options: [
        .workingColorSpace: CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!,
        .workingFormat: CIFormat.RGBAh,
        .cacheIntermediates: false,
    ])
}

/// 現像 1 件。preview / full は同じ処理で、差は scaleFactor だけ。
/// `context` を渡すとそれでレンダする（`--batch` の使い回し）。nil なら 1 件用に作る。
/// CIRAWFilter は毎回作る（使い回さない理由は README「バッチモード」）。
public func develop(_ request: DevelopRequest, context sharedContext: CIContext? = nil) throws -> SuccessResponse {
    try PathValidator.validate(request)
    let t0 = DispatchTime.now()

    let configured = try configureRAWFilter(
        input: request.input, maxEdge: request.maxEdge, decoderVersion: request.decoderVersion,
        settings: request.settings, adjustments: request.adjustments
    )
    let filter = configured.filter
    let native = configured.native
    let tCreated = configured.created
    let resolved = configured.resolved
    let adjustmentsPlan = configured.plan
    let guards = configured.guards

    // 診断モード: linearSpaceFilter は outputImage の取得より前に設定する（diagnostics 指定時のみ）。
    if let linear = request.diagnostics?.linearSpaceFilter {
        filter.linearSpaceFilter = linear.makeFilter()
    }

    // outputImage の参照はここ 1 回だけ。同じ filter で 2 回以上参照すると extent が半分ずつ縮む
    // （設計書 §2.2。macOS 27.0.1 で確認）ので、以降は `decoded` を使い回す。
    let tConfigured = DispatchTime.now()
    guard let decoded = filter.outputImage else {
        throw DevelopError(.renderFailed, "CIRAWFilter produced no output image")
    }
    // extent の検査は後段（長辺の Int 化・USM の長辺依存）より前に置く（無限・空の extent を Int() に渡さない）。
    guard isUsableExtent(decoded.extent) else {
        throw DevelopError(.renderFailed, "decoded image has an invalid extent")
    }
    var image = decoded
    // 診断モードの post_filters（outputImage → post_filters → adjustments の後段の順。設計書 §4.3）。
    var postFilterReports: [PostFilterReport]?
    if let postFilters = request.diagnostics?.postFilters {
        (image, postFilterReports) = try DiagnosticsPipeline.applyPostFilters(decoded, postFilters)
    }
    var adjustmentsApplied: AdjustmentsApplied?
    if let adjustments = resolved?.adjustments, let effect = resolved?.effect, let plan = adjustmentsPlan {
        (image, adjustmentsApplied) = try applyAdjustmentsPost(
            to: image, decodedExtent: decoded.extent, iso: isoSpeed(from: filter.properties),
            adjustments: adjustments, effect: effect, plan: plan, guards: guards
        )
    }
    guard isUsableExtent(image.extent) else {
        throw DevelopError(.renderFailed, "output image has an invalid extent")
    }
    let extent = image.extent.integral
    let t1 = DispatchTime.now()

    let (colorSpace, colorSpaceName) = OutputColor.space(for: request.format, colorSpace: request.outputColorSpace)
    let context = sharedContext ?? makeRenderContext()
    let outputWidth: Int
    let outputHeight: Int
    let written: Bool
    // 計測フック（GEOFRAME_CI_TIMING=1 のときだけ stderr へ。Issue #102）用の時刻。skip_write では nil のまま。
    var tRenderStart: DispatchTime?
    var tRendered: DispatchTime?
    var tWritten: DispatchTime?
    if request.diagnostics?.skipWrite == true {
        // 診断モードの skip_write: 画像のレンダ・エンコード・書き込みをしない（output のパス検証は済んでいる）。
        outputWidth = Int(extent.width)
        outputHeight = Int(extent.height)
        written = false
    } else {
        let cgImage: CGImage
        tRenderStart = DispatchTime.now()
        switch request.format {
        case .jpeg:
            guard let rendered = context.createCGImage(image, from: extent, format: .RGBA8, colorSpace: colorSpace) else {
                throw DevelopError(.renderFailed, "Core Image failed to render the image")
            }
            cgImage = rendered
        case .tiff16:
            cgImage = try renderRGB16(image, extent: extent, colorSpace: colorSpace, context: context)
        }

        tRendered = DispatchTime.now()
        try AtomicImageWriter.write(cgImage, to: request.output, format: request.format, jpegQuality: request.jpegQuality)
        tWritten = DispatchTime.now()
        outputWidth = cgImage.width
        outputHeight = cgImage.height
        written = true
    }

    // 診断モード: 統計・float dump（レンダ直前の `image` を RGBAf で描画する）。時間は render_encode に含める。
    var diagnosticsReport: DiagnosticsReport?
    if let diagnostics = request.diagnostics {
        diagnosticsReport = try DiagnosticsPipeline.run(
            diagnostics, image: image, extent: extent, context: context, written: written,
            filter: filter, decodedExtent: decoded.extent, postFilterReports: postFilterReports
        )
    }
    let t2 = DispatchTime.now()
    if StageTiming.enabled {
        StageTiming.report(StageTiming.line(
            t0: t0, created: tCreated, configured: tConfigured, graphBuilt: t1,
            renderStart: tRenderStart, rendered: tRendered, written: tWritten, end: t2
        ))
    }

    let applied = makeApplied(filter, profile: request.settings.profile, format: request.format, colorSpaceName: colorSpaceName)
    let source = makeSourceInfo(filter, native: native)

    return SuccessResponse(
        cli_version: cliVersion,
        macos_version: macOSVersionString,
        decoder_version: filter.decoderVersion.rawValue,
        input: request.input.path,
        output: request.output.path,
        mode: request.mode.rawValue,
        format: request.format.rawValue,
        width: outputWidth,
        height: outputHeight,
        elapsed_ms: ElapsedMS(decode: millis(t0, t1), render_encode: millis(t1, t2), total: millis(t0, t2)),
        applied: applied,
        source: source,
        adjustments_applied: adjustmentsApplied,
        diagnostics: diagnosticsReport
    )
}

/// filter から読み戻した値の応答（`applied`）。`develop` と `op: decode` が共有する。
func makeApplied(_ filter: CIRAWFilter, profile: Profile, format: OutputFormat, colorSpaceName: String) -> Applied {
    let sourceOrientation = filter.orientation.rawValue
    return Applied(
        profile: profile.rawValue,
        decoder_version: filter.decoderVersion.rawValue,
        scale_factor: readable(filter.scaleFactor),
        exposure: readable(filter.exposure),
        baseline_exposure: readable(filter.baselineExposure),
        boost_amount: readable(filter.boostAmount),
        boost_shadow_amount: readable(filter.boostShadowAmount),
        shadow_bias: readable(filter.shadowBias),
        neutral_temperature: readable(filter.neutralTemperature),
        neutral_tint: readable(filter.neutralTint),
        sharpness_amount: readable(filter.sharpnessAmount),
        contrast_amount: readable(filter.contrastAmount),
        detail_amount: readable(filter.detailAmount),
        luminance_noise_reduction_amount: readable(filter.luminanceNoiseReductionAmount),
        color_noise_reduction_amount: readable(filter.colorNoiseReductionAmount),
        moire_reduction_amount: readable(filter.moireReductionAmount),
        local_tone_map_amount: readable(filter.localToneMapAmount),
        extended_dynamic_range_amount: readable(filter.extendedDynamicRangeAmount),
        gamut_mapping_enabled: filter.isGamutMappingEnabled,
        draft_mode_enabled: filter.isDraftModeEnabled,
        lens_correction_enabled: filter.isLensCorrectionEnabled,
        lens_correction_supported: filter.isLensCorrectionSupported,
        local_tone_map_supported: filter.isLocalToneMapSupported,
        orientation: sourceOrientation,
        working_color_space: "extended linear sRGB (RGBAh)",
        color_space: colorSpaceName,
        bit_depth: format == .jpeg ? 8 : 16,
        orientation_handling: "CIRAWFilter applies the source EXIF orientation (\(sourceOrientation)) to outputImage; pixels are written upright and the output Orientation tag is 1 (no second rotation)"
    )
}

func makeSourceInfo(_ filter: CIRAWFilter, native: CGSize) -> SourceInfo {
    let properties = filter.properties
    let tiff = properties[kCGImagePropertyTIFFDictionary as String] as? [String: Any]
    let exif = properties[kCGImagePropertyExifDictionary as String] as? [String: Any]
    return SourceInfo(
        camera_model: tiff?[kCGImagePropertyTIFFModel as String] as? String,
        lens_model: exif?[kCGImagePropertyExifLensModel as String] as? String,
        iso: isoSpeed(from: properties),
        native_width: Int(native.width),
        native_height: Int(native.height),
        orientation: filter.orientation.rawValue
    )
}

/// 後段チェーン（PostChain）を掛け、`adjustments_applied` を組み立てる（`develop` と `op: render` が共有する）。
/// `decodedExtent` は CIRAWFilter.outputImage の extent（USM の長辺の根拠）、`iso` は撮影 ISO。
/// `adjustments` は hr の寄与を合成済みの実効値。
func applyAdjustmentsPost(
    to image: CIImage, decodedExtent rawExtent: CGRect, iso: Int?,
    adjustments: Adjustments, effect: HighlightRecoveryEffect, plan: AdjustmentsDecoderPlan, guards: [String]
) throws -> (CIImage, AdjustmentsApplied) {
    let decodedExtent = rawExtent.integral
    let longEdge = Int(max(decodedExtent.width, decodedExtent.height))
    let base = captureSharpenBase(longEdge: longEdge, iso: iso.map(Double.init))
    let params = PostChainParameters(
        // 実効 lift は合成時に [0, 100] へクランプ済み。後段カーネルへ渡す [0, 1] も明示的に止める。
        lift: min(1.0, max(0.0, adjustments.shadowLift / 100.0)),
        brightness: adjustments.brightness,
        contrast: adjustments.contrast,
        saturation: adjustments.saturation,
        usmSigma: base.sigma,
        usmGain: usmGainForCoreImage(sharpness: adjustments.sharpness, base: base),
        usmCoring: base.coring
    )
    let processed = try PostChain.apply(image, params)
    let applied = AdjustmentsApplied(
        defaults: plan.defaults.encodable,
        post: AdjustmentsApplied.Post(
            shadow_lift: params.lift,
            brightness: params.brightness,
            contrast: params.contrast,
            saturation: params.saturation,
            usm: AdjustmentsApplied.USM(
                sigma: params.usmSigma, gain: params.usmGain, coring: params.usmCoring, long_edge: longEdge, iso: iso
            ),
            wb: AdjustmentsApplied.WB(
                k0: plan.defaults.neutralTemperature, mired_shift: plan.miredShift, mired_per_unit: WhiteBalanceMapping.miredPerUnit
            ),
            noise_reduction: adjustments.noiseReduction.rawValue
        ),
        highlight_recovery: AdjustmentsApplied.HighlightRecovery(effect),
        ignored: Adjustments.ignoredKeys,
        guards: guards
    )
    return (processed, applied)
}

/// `configureRAWFilter` の結果。
struct ConfiguredRAWFilter {
    let filter: CIRAWFilter
    let native: CGSize
    /// `CIRAWFilter(imageURL:)` を作り終えた時刻（計測フック用）。
    let created: DispatchTime
    /// hr の寄与を合成した adjustments（`adjustments` 指定時のみ）。
    let resolved: (adjustments: Adjustments, effect: HighlightRecoveryEffect)?
    let plan: AdjustmentsDecoderPlan?
    let guards: [String]
}

/// CIRAWFilter を作り、decoderVersion → scaleFactor → profile → adjustments のデコード段 → 個別上書き → 白点ガード
/// の順に設定する（`develop` と `--batch` の `op: decode` が共有する。順序は変えないこと）。
func configureRAWFilter(
    input: URL, maxEdge: Int?, decoderVersion requestedDecoderVersion: String?, settings: Settings, adjustments: Adjustments?
) throws -> ConfiguredRAWFilter {
    guard let filter = CIRAWFilter(imageURL: input) else {
        throw DevelopError(.unsupportedRaw, "Core Image cannot decode this RAW file")
    }
    let tCreated = DispatchTime.now()

    // decoderVersion は最初に設定する。設定すると他のパラメータが既定値へ戻る
    // （実測: sharpnessAmount=0 → decoderVersion 設定 → 1.0 に戻った）。
    let supported = filter.supportedDecoderVersions.map(\.rawValue)
    let decoderVersion: String
    if let requested = requestedDecoderVersion {
        guard supported.contains(requested) else {
            throw DevelopError(.unsupportedDecoderVersion, "decoder_version \(requested) is not supported for this file (supported: \(supported.joined(separator: ", ")))")
        }
        decoderVersion = requested
    } else {
        guard let latest = supported.max(by: { (Int($0) ?? 0) < (Int($1) ?? 0) }) else {
            throw DevelopError(.unsupportedRaw, "no decoder version is available for this file")
        }
        decoderVersion = latest
    }
    filter.decoderVersion = CIRAWDecoderVersion(rawValue: decoderVersion)

    let native = filter.nativeSize
    let longEdge = max(native.width, native.height)
    guard longEdge > 0 else {
        throw DevelopError(.unsupportedRaw, "RAW file reports an empty native size")
    }
    var scale: Float = 1.0
    if let maxEdge = maxEdge {
        scale = min(1.0, Float(Double(maxEdge) / Double(longEdge)))
    }
    filter.scaleFactor = scale
    filter.isDraftModeEnabled = false

    applyProfile(settings, to: filter)

    // adjustments 指定時だけ、設計書 §3.2 の手順 3〜6 と 8 を挟む。無ければ 0.2.0 と同じ処理（素通し）。
    var adjustmentsPlan: AdjustmentsDecoderPlan?
    var guards: [String] = []
    // highlight_recovery の寄与を exposure / shadow_lift に加算した実効値（#81、設計書 §2・§3）。
    // 以降のデコード段・後段はこの実効値だけを見る（hr 0 は恒等）。
    let resolved = adjustments.map(HighlightRecoveryMapping.resolve)
    if let adjustments = resolved?.adjustments {
        let defaults = readDefaults(filter)
        let plan = applyAdjustmentsDecoder(adjustments, defaults: defaults, to: filter)
        adjustmentsPlan = plan
        guards.append(contentsOf: plan.guards)
        applyOverrides(settings, to: filter)
        // 白点ガード: 白点ガード ＞ 明示の settings.* ＞ adjustments の写像 ＞ profile（§4.2 の優先順位）。
        if WhiteDotGuard.needsGuard(
            luminanceNoiseReduction: filter.luminanceNoiseReductionAmount,
            sharpness: filter.sharpnessAmount,
            scaleFactor: filter.scaleFactor
        ) {
            filter.sharpnessAmount = max(filter.sharpnessAmount, WhiteDotGuard.sharpnessFloor)
            guards.append(WhiteDotGuard.name)
        }
    } else {
        applyOverrides(settings, to: filter)
    }

    return ConfiguredRAWFilter(
        filter: filter, native: native, created: tCreated, resolved: resolved, plan: adjustmentsPlan, guards: guards
    )
}

/// filter.properties の EXIF から撮影 ISO を読む（無ければ nil）。
func isoSpeed(from properties: [AnyHashable: Any]) -> Int? {
    let exif = properties[kCGImagePropertyExifDictionary as String] as? [String: Any]
    return (exif?[kCGImagePropertyExifISOSpeedRatings as String] as? [NSNumber])?.first?.intValue
}

/// profile 適用直後に filter から読み戻した既定値（§3.2 手順 3）。
public struct DecoderDefaults: Equatable {
    public var sharpnessAmount: Double
    public var luminanceNoiseReduction: Double
    public var colorNoiseReduction: Double
    public var neutralTemperature: Double
    public var neutralTint: Double

    var encodable: AdjustmentsApplied.Defaults {
        AdjustmentsApplied.Defaults(
            sharpness_amount: sharpnessAmount,
            luminance_noise_reduction_amount: luminanceNoiseReduction,
            color_noise_reduction_amount: colorNoiseReduction,
            neutral_temperature: neutralTemperature,
            neutral_tint: neutralTint
        )
    }
}

func readDefaults(_ filter: CIRAWFilter) -> DecoderDefaults {
    DecoderDefaults(
        sharpnessAmount: readable(filter.sharpnessAmount),
        luminanceNoiseReduction: readable(filter.luminanceNoiseReductionAmount),
        colorNoiseReduction: readable(filter.colorNoiseReductionAmount),
        neutralTemperature: readable(filter.neutralTemperature),
        neutralTint: readable(filter.neutralTint)
    )
}

/// `applyAdjustmentsDecoder` が filter に書いた内容の要約（応答用）。
struct AdjustmentsDecoderPlan {
    let defaults: DecoderDefaults
    let miredShift: Double
    /// デコード段で飛ばした写像の記録（`adjustments_applied.guards` に載る）。
    var guards: [String] = []
}

/// レンダに使える extent か（有限・非空。`CIImage.infinite` や空画像を Int 化・レンダへ渡さない）。
func isUsableExtent(_ extent: CGRect) -> Bool {
    // CGRect.infinite は成分が有限（±greatestFiniteMagnitude/2）なので isInfinite で別に弾く。
    guard !extent.isInfinite, !extent.isNull, !extent.isEmpty else { return false }
    let integral = extent.integral
    return extent.origin.x.isFinite && extent.origin.y.isFinite && extent.width.isFinite && extent.height.isFinite
        && integral.width > 0 && integral.height > 0 && integral.width.isFinite && integral.height.isFinite
}

/// adjustments のデコード段（exposure / wb_temp / sharpness ≤ 1 / noise_reduction）を filter へ書く（§3.3）。
/// 既定値と同じになる指定（wb_temp 0、sharpness 1.0、noise_reduction light）は filter に書かない
/// （Float ↔ K の往復などで「変えていないのに画が変わる」ことを避けるため）。
/// `adjustments` は hr の寄与を合成済みの実効値（`HighlightRecoveryMapping.resolve`）を渡すこと。
func applyAdjustmentsDecoder(_ adjustments: Adjustments, defaults: DecoderDefaults, to filter: CIRAWFilter) -> AdjustmentsDecoderPlan {
    // exposure: CIRAWFilter.exposure（EV、1:1）。settings.exposure_ev とは排他なので profile 側は 0。
    filter.exposure = Float(adjustments.exposure)

    // wb_temp: 撮影時 WB の K を mired でずらす。tint は触らない。
    var miredShift = 0.0
    var guards: [String] = []
    if adjustments.wbTemp != 0 {
        if WhiteBalanceMapping.isUsableK0(defaults.neutralTemperature) {
            let shifted = WhiteBalanceMapping.shift(k0: defaults.neutralTemperature, wbTemp: adjustments.wbTemp)
            filter.neutralTemperature = Float(shifted.kelvin)
            miredShift = shifted.miredShift
        } else {
            // 読み戻した K0 が使えない（NaN・範囲外）: WB の写像は飛ばして記録だけ残す。
            guards.append(WhiteBalanceMapping.invalidK0Guard)
        }
    }

    // sharpness ≤ 1: デコーダ内シャープを既定 × s に弱める。> 1 は既定のまま（増分は後段 USM）。
    if let amount = decoderSharpnessAmount(sharpness: adjustments.sharpness, defaultAmount: defaults.sharpnessAmount) {
        filter.sharpnessAmount = Float(amount)
    }

    // noise_reduction: off / light（既定のまま）/ full。
    let nr = NoiseReductionMapping.amounts(
        adjustments.noiseReduction,
        defaultLuminance: defaults.luminanceNoiseReduction,
        defaultColor: defaults.colorNoiseReduction
    )
    if let luminance = nr.luminance { filter.luminanceNoiseReductionAmount = Float(luminance) }
    if let color = nr.color { filter.colorNoiseReductionAmount = Float(color) }

    // highlight_recovery: ここでは見ない（呼び出し側で exposure / shadow_lift に合成済み。#81）。
    return AdjustmentsDecoderPlan(defaults: defaults, miredShift: miredShift, guards: guards)
}

/// profile / settings を filter へ設定する（decoderVersion 設定後に呼ぶこと）。
func applyProfile(_ settings: Settings, to filter: CIRAWFilter) {
    switch settings.profile {
    case .appleDefault:
        break
    case .neutral:
        // ADR 0001「中立プロファイル」: 局所的・装飾的な処理だけを 0 にし、
        // 表示用トーンカーブ（boost / boostShadow / baselineExposure / shadowBias）は既定のまま残す。
        filter.sharpnessAmount = 0
        filter.contrastAmount = 0
        filter.detailAmount = 0
        filter.luminanceNoiseReductionAmount = 0
        filter.colorNoiseReductionAmount = 0
        filter.moireReductionAmount = 0
        filter.extendedDynamicRangeAmount = 0
        if filter.isLocalToneMapSupported {
            filter.localToneMapAmount = 0
        }
    }
    filter.exposure = Float(settings.exposureEV)
    if let kelvin = settings.neutralTemperature {
        filter.neutralTemperature = Float(kelvin)
    }
    if let tint = settings.neutralTint {
        filter.neutralTint = Float(tint)
    }
    // CIRAWFilter の既定は ON だが、この CLI では明示指定時だけ ON にする（両 profile 共通）。
    filter.isLensCorrectionEnabled = settings.lensCorrection
    filter.isDraftModeEnabled = false
}

/// settings の個別上書き（Issue #79）。profile 適用後に、指定されたキーだけを filter へ書く。
/// `apple_default` では OS が決めた既定値を、`neutral` では 0 をそれぞれ置き換えることになる。
/// 実際に効いた値は `applied` に読み戻しで載る（例: extendedDynamicRangeAmount は実測で 2.0 に丸められる）。
func applyOverrides(_ settings: Settings, to filter: CIRAWFilter) {
    if let value = settings.luminanceNoiseReduction { filter.luminanceNoiseReductionAmount = Float(value) }
    if let value = settings.colorNoiseReduction { filter.colorNoiseReductionAmount = Float(value) }
    if let value = settings.sharpnessAmount { filter.sharpnessAmount = Float(value) }
    if let value = settings.detailAmount { filter.detailAmount = Float(value) }
    if let value = settings.extendedDynamicRange { filter.extendedDynamicRangeAmount = Float(value) }
    if let value = settings.boostAmount { filter.boostAmount = Float(value) }
    if let value = settings.boostShadowAmount { filter.boostShadowAmount = Float(value) }
    // 診断用（CLI 0.4.0、Issue #92 §4.2）。省略時は書かない（既定リクエストに影響しない）。
    if let value = settings.gamutMapping { filter.isGamutMappingEnabled = value }
    if let value = settings.baselineExposure { filter.baselineExposure = Float(value) }
}

/// 16bit/チャンネル・アルファ無しの RGB として描画する（TIFF に余計な alpha を書かないため）。
func renderRGB16(_ image: CIImage, extent: CGRect, colorSpace: CGColorSpace, context: CIContext) throws -> CGImage {
    let width = Int(extent.width)
    let height = Int(extent.height)
    let rowBytes = width * 8
    guard let buffer = CFDataCreateMutable(nil, rowBytes * height) else {
        throw DevelopError(.renderFailed, "failed to allocate the render buffer")
    }
    CFDataSetLength(buffer, rowBytes * height)
    guard let base = CFDataGetMutableBytePtr(buffer) else {
        throw DevelopError(.renderFailed, "failed to allocate the render buffer")
    }
    context.render(image, toBitmap: base, rowBytes: rowBytes, bounds: extent, format: .RGBA16, colorSpace: colorSpace)
    let bitmapInfo = CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue | CGBitmapInfo.byteOrder16Little.rawValue)
    guard
        let provider = CGDataProvider(data: buffer),
        let cgImage = CGImage(
            width: width, height: height, bitsPerComponent: 16, bitsPerPixel: 64, bytesPerRow: rowBytes,
            space: colorSpace, bitmapInfo: bitmapInfo, provider: provider, decode: nil,
            shouldInterpolate: false, intent: .defaultIntent
        )
    else {
        throw DevelopError(.renderFailed, "failed to build a 16-bit image")
    }
    return cgImage
}

/// output と同じディレクトリの一時ファイルへ書き、rename(2) で置き換える。
/// 失敗時は一時ファイルを消し、既存の output には触れない。
public enum AtomicImageWriter {
    public static func write(_ image: CGImage, to output: URL, format: OutputFormat, jpegQuality: Double?) throws {
        var data = try encode(image, format: format, jpegQuality: jpegQuality)
        if format == .jpeg {
            // ImageIO は sRGB の JPEG に ICC を埋めず EXIF ColorSpace=1 だけを書く（実測:
            // 名前付き sRGB・ICC 由来の sRGB・CIContext.jpegRepresentation のいずれも ICC 0 byte）。
            // 色管理しないビューアでも sRGB と解釈させるため、APP2 ICC_PROFILE を明示的に挿入する。
            guard let icc = CGColorSpace(name: CGColorSpace.sRGB)?.copyICCData() as Data? else {
                throw DevelopError(.writeFailed, "failed to obtain the sRGB ICC profile")
            }
            data = try JPEGICC.embed(icc, into: data)
        }
        try writeAtomically(data, to: output)
    }

    static func encode(_ image: CGImage, format: OutputFormat, jpegQuality: Double?) throws -> Data {
        let type: UTType = format == .jpeg ? .jpeg : .tiff
        // 最小メタデータのみ（ソフトウェア名と向き=1）。元 RAW の EXIF / GPS は引き継がない。
        var properties: [CFString: Any] = [
            kCGImagePropertyOrientation: 1,
            kCGImagePropertyTIFFDictionary: [
                kCGImagePropertyTIFFSoftware: "geoframe-ci-develop \(cliVersion)",
                kCGImagePropertyTIFFOrientation: 1,
            ] as [CFString: Any],
        ]
        if format == .jpeg {
            properties[kCGImageDestinationLossyCompressionQuality] = jpegQuality ?? RequestParser.defaultJPEGQuality
        }
        let buffer = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(buffer as CFMutableData, type.identifier as CFString, 1, nil) else {
            throw DevelopError(.writeFailed, "cannot create an image encoder for \(format.rawValue)")
        }
        CGImageDestinationAddImage(destination, image, properties as CFDictionary)
        guard CGImageDestinationFinalize(destination) else {
            throw DevelopError(.writeFailed, "failed to encode \(format.rawValue)")
        }
        return buffer as Data
    }

    public static func writeAtomically(_ data: Data, to output: URL) throws {
        let directory = output.deletingLastPathComponent()
        let temp = directory.appendingPathComponent(".\(output.lastPathComponent).\(UUID().uuidString).tmp")
        do {
            try data.write(to: temp, options: .withoutOverwriting)
        } catch {
            unlink(temp.path)
            throw DevelopError(.writeFailed, "cannot write a temporary file in \(directory.path): \(error.localizedDescription)")
        }
        guard rename(temp.path, output.path) == 0 else {
            let reason = String(cString: strerror(errno))
            unlink(temp.path)
            throw DevelopError(.writeFailed, "failed to move the output into place: \(reason)")
        }
    }
}

/// JPEG のバイト列へ APP2 ICC_PROFILE セグメントを挿入する（ICC Spec Annex B.4）。
public enum JPEGICC {
    static let signature = Array("ICC_PROFILE".utf8) + [0]

    public static func embed(_ icc: Data, into jpeg: Data) throws -> Data {
        let bytes = [UInt8](jpeg)
        guard bytes.count >= 4, bytes[0] == 0xFF, bytes[1] == 0xD8 else {
            throw DevelopError(.writeFailed, "encoder did not produce a JPEG stream")
        }
        // 1 セグメント（最大 65533 - 16 byte）に収まる ICC だけを扱う（sRGB は約 3KB）。
        let payloadLength = signature.count + 2 + icc.count
        guard payloadLength + 2 <= 0xFFFF else {
            throw DevelopError(.writeFailed, "ICC profile is too large for a single APP2 segment")
        }
        // SOI 直後の APP0(JFIF) / APP1(EXIF) の後ろへ置く。既に ICC があれば何もしない。
        var index = 2
        while index + 4 <= bytes.count, bytes[index] == 0xFF {
            let marker = bytes[index + 1]
            let length = Int(bytes[index + 2]) << 8 | Int(bytes[index + 3])
            if marker == 0xE2, index + 4 + signature.count <= bytes.count,
               Array(bytes[(index + 4)..<(index + 4 + signature.count)]) == signature {
                return jpeg
            }
            guard marker == 0xE0 || marker == 0xE1, length >= 2, index + 2 + length <= bytes.count else { break }
            index += 2 + length
        }
        var segment: [UInt8] = [0xFF, 0xE2]
        let segmentLength = payloadLength + 2
        segment += [UInt8(segmentLength >> 8), UInt8(segmentLength & 0xFF)]
        segment += signature
        segment += [1, 1]  // sequence number, total chunks
        segment += [UInt8](icc)
        var result = Data(bytes[0..<index])
        result.append(contentsOf: segment)
        result.append(contentsOf: bytes[index...])
        return result
    }
}

// MARK: - --version / --capabilities

public struct VersionInfo: Encodable {
    public let ok = true
    public let engine = "coreimage"
    public let cli_version = cliVersion
    public let macos_version = macOSVersionString

    public init() {}
}

public struct Capabilities: Encodable {
    public let ok = true
    public let engine = "coreimage"
    public let cli_version = cliVersion
    public let macos_version = macOSVersionString
    public let ilce6400_supported: Bool
    public let camera_model_names: [String]
    public let request_decoder_versions: [String]
    /// supportedDecoderVersions はファイル単位の API。probe ファイルを渡したときだけ埋まる。
    public let supported_decoder_versions: [String]?
    public let probe_input: String?
    public let output_formats: [String: String]
    public let profiles: [String]
    public let adjustments: AdjustmentsCapability
    /// 診断モード（CLI 0.4.0、Issue #92）。本番では使わない。
    public let diagnostics: DiagnosticsCapability

    public init(probe: URL?) {
        let models = CIRAWFilter.supportedCameraModels
        let matches = models.filter { $0.contains("ILCE-6400") }.sorted()
        ilce6400_supported = models.contains(ilce6400ModelName)
        camera_model_names = matches
        request_decoder_versions = RequestParser.allowedDecoderVersions.sorted()
        if let probe, let filter = CIRAWFilter(imageURL: probe) {
            supported_decoder_versions = filter.supportedDecoderVersions.map(\.rawValue)
            probe_input = probe.path
        } else {
            supported_decoder_versions = nil
            probe_input = probe?.path
        }
        output_formats = [
            "jpeg": "8-bit JPEG, sRGB IEC61966-2.1 ICC embedded",
            "tiff16": "16-bit RGB TIFF, Display P3 ICC embedded (default) or sRGB with output_color_space \"srgb\"",
        ]
        profiles = [Profile.neutral.rawValue, Profile.appleDefault.rawValue]
        adjustments = AdjustmentsCapability()
        diagnostics = DiagnosticsCapability()
    }
}

/// `--capabilities` の `adjustments`（設計書 §4.2）。
public struct AdjustmentsCapability: Encodable {
    public let supported = true
    public let ignored = Adjustments.ignoredKeys
    public let post_kernel = "metal"

    public init() {}
}
