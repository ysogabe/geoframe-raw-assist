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
    public let color_space: String
    public let bit_depth: Int
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
}

public struct ErrorBody: Encodable {
    public let code: ErrorCode
    public let message: String
}

public struct FailureResponse: Encodable {
    public let ok = false
    public let error: ErrorBody

    public init(_ error: DevelopError) {
        self.error = ErrorBody(code: error.code, message: error.message)
    }
}

public func encodeJSONLine<T: Encodable>(_ value: T) -> String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
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
    static func space(for format: OutputFormat) -> (CGColorSpace, String) {
        switch format {
        case .jpeg:
            return (CGColorSpace(name: CGColorSpace.sRGB)!, "sRGB IEC61966-2.1")
        case .tiff16:
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

/// 現像 1 件。preview / full は同じ処理で、差は scaleFactor だけ。
public func develop(_ request: DevelopRequest) throws -> SuccessResponse {
    try PathValidator.validate(request)
    let t0 = DispatchTime.now()

    guard let filter = CIRAWFilter(imageURL: request.input) else {
        throw DevelopError(.unsupportedRaw, "Core Image cannot decode this RAW file")
    }

    // decoderVersion は最初に設定する。設定すると他のパラメータが既定値へ戻る
    // （実測: sharpnessAmount=0 → decoderVersion 設定 → 1.0 に戻った）。
    let supported = filter.supportedDecoderVersions.map(\.rawValue)
    let decoderVersion: String
    if let requested = request.decoderVersion {
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
    if let maxEdge = request.maxEdge {
        scale = min(1.0, Float(Double(maxEdge) / Double(longEdge)))
    }
    filter.scaleFactor = scale
    filter.isDraftModeEnabled = false

    applyProfile(request.settings, to: filter)

    guard let image = filter.outputImage else {
        throw DevelopError(.renderFailed, "CIRAWFilter produced no output image")
    }
    let extent = image.extent.integral
    guard extent.width > 0, extent.height > 0, extent.width.isFinite, extent.height.isFinite else {
        throw DevelopError(.renderFailed, "output image has an invalid extent")
    }
    let t1 = DispatchTime.now()

    let (colorSpace, colorSpaceName) = OutputColor.space(for: request.format)
    let context = CIContext(options: [
        .workingColorSpace: CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!,
        .workingFormat: CIFormat.RGBAh,
        .cacheIntermediates: false,
    ])
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

    let properties = filter.properties
    let tiff = properties[kCGImagePropertyTIFFDictionary as String] as? [String: Any]
    let exif = properties[kCGImagePropertyExifDictionary as String] as? [String: Any]
    let iso = (exif?[kCGImagePropertyExifISOSpeedRatings as String] as? [NSNumber])?.first?.intValue
    let sourceOrientation = filter.orientation.rawValue

    let applied = Applied(
        profile: request.settings.profile.rawValue,
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
        bit_depth: request.format == .jpeg ? 8 : 16,
        orientation_handling: "CIRAWFilter applies the source EXIF orientation (\(sourceOrientation)) to outputImage; pixels are written upright and the output Orientation tag is 1 (no second rotation)"
    )

    return SuccessResponse(
        cli_version: cliVersion,
        macos_version: macOSVersionString,
        decoder_version: filter.decoderVersion.rawValue,
        input: request.input.path,
        output: request.output.path,
        mode: request.mode.rawValue,
        format: request.format.rawValue,
        width: cgImage.width,
        height: cgImage.height,
        elapsed_ms: ElapsedMS(decode: millis(t0, t1), render_encode: millis(t1, t2), total: millis(t0, t2)),
        applied: applied,
        source: SourceInfo(
            camera_model: tiff?[kCGImagePropertyTIFFModel as String] as? String,
            lens_model: exif?[kCGImagePropertyExifLensModel as String] as? String,
            iso: iso,
            native_width: Int(native.width),
            native_height: Int(native.height),
            orientation: sourceOrientation
        )
    )
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
        output_formats = ["jpeg": "8-bit JPEG, sRGB IEC61966-2.1 ICC embedded", "tiff16": "16-bit RGB TIFF, Display P3 ICC embedded"]
        profiles = [Profile.neutral.rawValue, Profile.appleDefault.rawValue]
    }
}
