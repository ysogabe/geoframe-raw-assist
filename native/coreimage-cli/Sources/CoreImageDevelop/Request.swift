import Foundation

/// CLI のバージョン。応答 JSON の `cli_version` に載る。
public let cliVersion = "0.4.0"

/// 失敗応答の `error.code`。README のエラーコード表と一致させること。
public enum ErrorCode: String, Encodable {
    case invalidRequest = "INVALID_REQUEST"
    case inputNotFound = "INPUT_NOT_FOUND"
    case inputNotReadable = "INPUT_NOT_READABLE"
    case outputDirNotFound = "OUTPUT_DIR_NOT_FOUND"
    case unsupportedRaw = "UNSUPPORTED_RAW"
    case unsupportedDecoderVersion = "UNSUPPORTED_DECODER_VERSION"
    case renderFailed = "RENDER_FAILED"
    case writeFailed = "WRITE_FAILED"
    case internalError = "INTERNAL_ERROR"
}

public struct DevelopError: Error, Equatable {
    public let code: ErrorCode
    public let message: String

    public init(_ code: ErrorCode, _ message: String) {
        self.code = code
        self.message = message
    }
}

public enum Mode: String {
    case preview
    case full
}

public enum OutputFormat: String {
    case jpeg
    case tiff16
}

/// `tiff16` の出力色空間（Issue #79）。jpeg は常に sRGB なので指定できない。
public enum OutputColorSpace: String {
    case srgb
    /// 互換のための既定（0.1.0 までの tiff16 は常に Display P3 だった）。
    case displayP3 = "display_p3"
}

public enum Profile: String {
    /// 表示用 sRGB として妥当な最小処理（ADR 0001 §中立プロファイル）。
    case neutral
    /// CIRAWFilter の既定値のまま（lens_correction だけ settings に従う）。
    case appleDefault = "apple_default"
}

public struct Settings: Equatable {
    public var exposureEV: Double = 0.0
    public var neutralTemperature: Double?
    public var neutralTint: Double?
    public var lensCorrection: Bool = false
    public var profile: Profile = .neutral
    /// 以下は profile 適用後に個別に上書きする値（Issue #79）。nil = profile の既定値のまま。
    /// 値域は CIRAWFilter.h のコメントに従う（README「settings の個別上書き」）。
    public var luminanceNoiseReduction: Double?
    public var colorNoiseReduction: Double?
    public var sharpnessAmount: Double?
    public var detailAmount: Double?
    public var extendedDynamicRange: Double?
    public var boostAmount: Double?
    public var boostShadowAmount: Double?
    /// 診断用の CIRAWFilter プロパティ上書き（CLI 0.4.0、Issue #92 設計書 §4.2）。nil = CIRAWFilter の既定のまま。
    /// 本番（アプリ）では使わない。
    public var gamutMapping: Bool?
    public var baselineExposure: Double?
}

/// 検証済みの現像リクエスト。`RequestParser.parse` だけが作る。
public struct DevelopRequest: Equatable {
    public var input: URL
    public var output: URL
    public var mode: Mode
    /// preview のときだけ値を持つ（full は常に nil = 等倍）。
    public var maxEdge: Int?
    public var format: OutputFormat
    /// jpeg のときだけ値を持つ。
    public var jpegQuality: Double?
    /// tiff16 のときだけ値を持つ（省略時 display_p3）。jpeg は常に sRGB で nil。
    public var outputColorSpace: OutputColorSpace?
    /// nil = そのファイルで使える最新の decoderVersion。
    public var decoderVersion: String?
    public var settings: Settings
    /// アプリのスライダー 9 キー（Issue #73）。nil = 指定なし（0.2.0 と同じ処理）。
    public var adjustments: Adjustments? = nil
    /// 診断モード（CLI 0.4.0、Issue #92 設計書 §4.3）。nil = 通常モード（0.3.0 と同じ処理）。
    public var diagnostics: Diagnostics? = nil
}

/// stdin の JSON を構文・値域だけで検証する（ファイルシステムには触れない）。
public enum RequestParser {
    public static let defaultMaxEdge = 2400
    public static let maxEdgeRange = 64...20000
    public static let defaultJPEGQuality = 0.92
    public static let exposureRange = -5.0...5.0
    public static let temperatureRange = 2000.0...50000.0
    public static let tintRange = -150.0...150.0
    public static let allowedDecoderVersions: Set<String> = ["8", "9"]
    /// 個別上書きキーの値域（CIRAWFilter.h のコメントどおり）。
    public static let noiseReductionRange = 0.0...1.0
    public static let sharpnessRange = 0.0...1.0
    public static let detailRange = 0.0...3.0
    public static let extendedDynamicRangeRange = 0.0...2.0
    public static let boostRange = 0.0...1.0
    public static let boostShadowRange = 0.0...2.0
    public static let baselineExposureRange = -5.0...5.0

    static let topLevelKeys: Set<String> = [
        "input", "output", "mode", "max_edge", "format", "jpeg_quality", "output_color_space", "decoder_version", "settings",
        "adjustments", "diagnostics",
    ]
    static let settingsKeys: Set<String> = [
        "exposure_ev", "neutral_temperature", "neutral_tint", "lens_correction", "profile",
        "luminance_noise_reduction", "color_noise_reduction", "sharpness_amount", "detail_amount",
        "extended_dynamic_range", "boost_amount", "boost_shadow_amount",
        "gamut_mapping", "baseline_exposure",
    ]
    /// settings の個別上書きキー → (Settings のフィールドを書く closure, 値域, 説明)。
    static let overrideKeys: [(key: String, range: ClosedRange<Double>, set: (inout Settings, Double) -> Void)] = [
        ("luminance_noise_reduction", noiseReductionRange, { $0.luminanceNoiseReduction = $1 }),
        ("color_noise_reduction", noiseReductionRange, { $0.colorNoiseReduction = $1 }),
        ("sharpness_amount", sharpnessRange, { $0.sharpnessAmount = $1 }),
        ("detail_amount", detailRange, { $0.detailAmount = $1 }),
        ("extended_dynamic_range", extendedDynamicRangeRange, { $0.extendedDynamicRange = $1 }),
        ("boost_amount", boostRange, { $0.boostAmount = $1 }),
        ("boost_shadow_amount", boostShadowRange, { $0.boostShadowAmount = $1 }),
        ("baseline_exposure", baselineExposureRange, { $0.baselineExposure = $1 }),
    ]

    public static func parse(_ data: Data) throws -> DevelopRequest {
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: data, options: [])
        } catch {
            throw DevelopError(.invalidRequest, "request is not valid JSON")
        }
        guard let dict = object as? [String: Any] else {
            throw DevelopError(.invalidRequest, "request must be a JSON object")
        }
        try rejectUnknownKeys(dict, allowed: topLevelKeys, context: "request")

        let input = try absolutePath(dict, "input")
        let output = try absolutePath(dict, "output")

        guard let modeRaw = dict["mode"] as? String, let mode = Mode(rawValue: modeRaw) else {
            throw DevelopError(.invalidRequest, "mode must be \"preview\" or \"full\"")
        }

        var maxEdge: Int?
        if mode == .preview {
            if let raw = dict["max_edge"], !(raw is NSNull) {
                guard let value = integer(raw), maxEdgeRange.contains(value) else {
                    throw DevelopError(.invalidRequest, "max_edge must be an integer in \(maxEdgeRange.lowerBound)...\(maxEdgeRange.upperBound)")
                }
                maxEdge = value
            } else {
                maxEdge = defaultMaxEdge
            }
        } else if let raw = dict["max_edge"], !(raw is NSNull) {
            throw DevelopError(.invalidRequest, "max_edge is only allowed with mode \"preview\"")
        }

        var format = OutputFormat.jpeg
        if let raw = dict["format"], !(raw is NSNull) {
            guard let string = raw as? String, let value = OutputFormat(rawValue: string) else {
                throw DevelopError(.invalidRequest, "format must be \"jpeg\" or \"tiff16\"")
            }
            format = value
        }

        let ext = output.pathExtension.lowercased()
        switch format {
        case .jpeg where !["jpg", "jpeg"].contains(ext):
            throw DevelopError(.invalidRequest, "output extension must be .jpg or .jpeg for format jpeg")
        case .tiff16 where !["tif", "tiff"].contains(ext):
            throw DevelopError(.invalidRequest, "output extension must be .tif or .tiff for format tiff16")
        default:
            break
        }

        var jpegQuality: Double?
        if format == .jpeg {
            if let raw = dict["jpeg_quality"], !(raw is NSNull) {
                guard let value = number(raw), (0.0...1.0).contains(value) else {
                    throw DevelopError(.invalidRequest, "jpeg_quality must be a number in 0...1")
                }
                jpegQuality = value
            } else {
                jpegQuality = defaultJPEGQuality
            }
        } else if let raw = dict["jpeg_quality"], !(raw is NSNull) {
            throw DevelopError(.invalidRequest, "jpeg_quality is only allowed with format \"jpeg\"")
        }

        var outputColorSpace: OutputColorSpace?
        if format == .tiff16 {
            if let raw = dict["output_color_space"], !(raw is NSNull) {
                guard let string = raw as? String, let value = OutputColorSpace(rawValue: string) else {
                    throw DevelopError(.invalidRequest, "output_color_space must be \"srgb\" or \"display_p3\"")
                }
                outputColorSpace = value
            } else {
                outputColorSpace = .displayP3
            }
        } else if let raw = dict["output_color_space"], !(raw is NSNull) {
            throw DevelopError(.invalidRequest, "output_color_space is only allowed with format \"tiff16\" (jpeg is always sRGB)")
        }

        var decoderVersion: String?
        if let raw = dict["decoder_version"], !(raw is NSNull) {
            guard let string = raw as? String, allowedDecoderVersions.contains(string) else {
                throw DevelopError(.invalidRequest, "decoder_version must be \"8\" or \"9\" (or omitted for the latest)")
            }
            decoderVersion = string
        }

        let settings = try parseSettings(dict["settings"])
        let adjustments = try parseAdjustments(dict["adjustments"])
        if adjustments != nil {
            try validateAdjustmentsAgainstSettings(dict["settings"], settings)
        }
        let diagnostics = try parseDiagnostics(dict["diagnostics"])

        guard input.pathExtension.lowercased() == "arw" else {
            throw DevelopError(.invalidRequest, "input must be a Sony .ARW file")
        }
        if input.standardizedFileURL.path == output.standardizedFileURL.path {
            throw DevelopError(.invalidRequest, "input and output must be different paths")
        }
        if let dump = diagnostics?.dumpFloat {
            if dump.path.path == input.standardizedFileURL.path {
                throw DevelopError(.invalidRequest, "diagnostics.dump_float.path must differ from input")
            }
            if dump.path.path == output.standardizedFileURL.path {
                throw DevelopError(.invalidRequest, "diagnostics.dump_float.path must differ from output")
            }
        }

        return DevelopRequest(
            input: input, output: output, mode: mode, maxEdge: maxEdge, format: format,
            jpegQuality: jpegQuality, outputColorSpace: outputColorSpace, decoderVersion: decoderVersion, settings: settings,
            adjustments: adjustments, diagnostics: diagnostics
        )
    }

    static func parseSettings(_ raw: Any?) throws -> Settings {
        var settings = Settings()
        guard let raw, !(raw is NSNull) else { return settings }
        guard let dict = raw as? [String: Any] else {
            throw DevelopError(.invalidRequest, "settings must be a JSON object")
        }
        try rejectUnknownKeys(dict, allowed: settingsKeys, context: "settings")

        if let value = dict["exposure_ev"], !(value is NSNull) {
            guard let ev = number(value), exposureRange.contains(ev) else {
                throw DevelopError(.invalidRequest, "settings.exposure_ev must be a number in -5...5")
            }
            settings.exposureEV = ev
        }
        if let value = dict["neutral_temperature"], !(value is NSNull) {
            guard let kelvin = number(value), temperatureRange.contains(kelvin) else {
                throw DevelopError(.invalidRequest, "settings.neutral_temperature must be null or a number in 2000...50000")
            }
            settings.neutralTemperature = kelvin
        }
        if let value = dict["neutral_tint"], !(value is NSNull) {
            guard let tint = number(value), tintRange.contains(tint) else {
                throw DevelopError(.invalidRequest, "settings.neutral_tint must be null or a number in -150...150")
            }
            settings.neutralTint = tint
        }
        if let value = dict["lens_correction"], !(value is NSNull) {
            guard let flag = boolean(value) else {
                throw DevelopError(.invalidRequest, "settings.lens_correction must be a boolean")
            }
            settings.lensCorrection = flag
        }
        if let value = dict["profile"], !(value is NSNull) {
            guard let string = value as? String, let profile = Profile(rawValue: string) else {
                throw DevelopError(.invalidRequest, "settings.profile must be \"neutral\" or \"apple_default\"")
            }
            settings.profile = profile
        }
        if let value = dict["gamut_mapping"], !(value is NSNull) {
            guard let flag = boolean(value) else {
                throw DevelopError(.invalidRequest, "settings.gamut_mapping must be null or a boolean")
            }
            settings.gamutMapping = flag
        }
        for entry in overrideKeys {
            guard let value = dict[entry.key], !(value is NSNull) else { continue }
            guard let amount = number(value), entry.range.contains(amount) else {
                throw DevelopError(
                    .invalidRequest,
                    "settings.\(entry.key) must be null or a number in \(entry.range.lowerBound)...\(entry.range.upperBound)"
                )
            }
            entry.set(&settings, amount)
        }
        return settings
    }

    /// `adjustments`（省略可・null は省略と同じ）。指定時は 9 キーすべて必須、未知キー・値域外・型違いは
    /// INVALID_REQUEST（クランプしない）。bool は数値として受けない。
    static func parseAdjustments(_ raw: Any?) throws -> Adjustments? {
        guard let raw, !(raw is NSNull) else { return nil }
        guard let dict = raw as? [String: Any] else {
            throw DevelopError(.invalidRequest, "adjustments must be a JSON object")
        }
        try rejectUnknownKeys(dict, allowed: Adjustments.allKeys, context: "adjustments")
        let missing = Adjustments.allKeys.filter { key in
            guard let value = dict[key] else { return true }
            return value is NSNull
        }.sorted()
        if !missing.isEmpty {
            throw DevelopError(.invalidRequest, "adjustments requires all 9 keys; missing: \(missing.joined(separator: ", "))")
        }
        var adjustments = Adjustments()
        for entry in Adjustments.numericKeys {
            guard let value = number(dict[entry.key]!), entry.range.contains(value) else {
                throw DevelopError(
                    .invalidRequest,
                    "adjustments.\(entry.key) must be a number in \(entry.range.lowerBound)...\(entry.range.upperBound)"
                )
            }
            entry.set(&adjustments, value)
        }
        guard let string = dict["noise_reduction"] as? String, let mode = NoiseReduction(rawValue: string) else {
            throw DevelopError(.invalidRequest, "adjustments.noise_reduction must be \"off\", \"light\" or \"full\"")
        }
        adjustments.noiseReduction = mode
        return adjustments
    }

    /// `adjustments` 指定時の settings との整合（設計書 §4.1）。
    /// - profile は apple_default のみ（写像が apple_default の既定値基準のため。省略時の neutral も不可）
    /// - settings.exposure_ev / neutral_temperature / neutral_tint は**値に関わらず指定自体**を拒否（null は省略扱い）
    /// - 個別上書き 7 キーは同時指定可（adjustments の写像の後に適用される）
    static func validateAdjustmentsAgainstSettings(_ rawSettings: Any?, _ settings: Settings) throws {
        guard settings.profile == .appleDefault else {
            throw DevelopError(.invalidRequest, "adjustments requires settings.profile \"apple_default\"")
        }
        guard let dict = rawSettings as? [String: Any] else { return }
        let conflicting = ["exposure_ev", "neutral_temperature", "neutral_tint"].filter { key in
            guard let value = dict[key] else { return false }
            return !(value is NSNull)
        }
        if !conflicting.isEmpty {
            throw DevelopError(
                .invalidRequest,
                "adjustments cannot be combined with settings.\(conflicting.joined(separator: ", settings."))"
            )
        }
    }

    static func rejectUnknownKeys(_ dict: [String: Any], allowed: Set<String>, context: String) throws {
        let unknown = Set(dict.keys).subtracting(allowed).sorted()
        if !unknown.isEmpty {
            throw DevelopError(.invalidRequest, "unknown \(context) key(s): \(unknown.joined(separator: ", "))")
        }
    }

    static func absolutePath(_ dict: [String: Any], _ key: String) throws -> URL {
        guard let path = dict[key] as? String, !path.isEmpty else {
            throw DevelopError(.invalidRequest, "\(key) must be a non-empty string")
        }
        guard path.hasPrefix("/") else {
            throw DevelopError(.invalidRequest, "\(key) must be an absolute path")
        }
        guard !path.contains("\u{0}") else {
            throw DevelopError(.invalidRequest, "\(key) must not contain NUL")
        }
        return URL(fileURLWithPath: path).standardizedFileURL
    }

    // JSONSerialization は true/false も NSNumber で返すので、Bool と数値を型で区別する。
    static func isBool(_ value: Any) -> Bool {
        guard let number = value as? NSNumber else { return false }
        return CFGetTypeID(number) == CFBooleanGetTypeID()
    }

    static func boolean(_ value: Any) -> Bool? {
        isBool(value) ? (value as? NSNumber)?.boolValue : nil
    }

    static func number(_ value: Any) -> Double? {
        guard !isBool(value), let number = value as? NSNumber else { return nil }
        let double = number.doubleValue
        return double.isFinite ? double : nil
    }

    static func integer(_ value: Any) -> Int? {
        guard let double = number(value), double.rounded() == double, abs(double) < 1e9 else { return nil }
        return Int(double)
    }
}

/// ファイルシステムに対する事前検証（読むだけ。書き込みはしない）。
public enum PathValidator {
    public static func validate(_ request: DevelopRequest, fileManager: FileManager = .default) throws {
        let inputPath = request.input.path
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: inputPath, isDirectory: &isDirectory) else {
            throw DevelopError(.inputNotFound, "input does not exist: \(inputPath)")
        }
        guard !isDirectory.boolValue else {
            throw DevelopError(.invalidRequest, "input is a directory: \(inputPath)")
        }
        guard fileManager.isReadableFile(atPath: inputPath) else {
            throw DevelopError(.inputNotReadable, "input is not readable: \(inputPath)")
        }

        let parent = request.output.deletingLastPathComponent().path
        var parentIsDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: parent, isDirectory: &parentIsDirectory), parentIsDirectory.boolValue else {
            throw DevelopError(.outputDirNotFound, "output directory does not exist: \(parent)")
        }

        var outputIsDirectory: ObjCBool = false
        if fileManager.fileExists(atPath: request.output.path, isDirectory: &outputIsDirectory) {
            if outputIsDirectory.boolValue {
                throw DevelopError(.invalidRequest, "output is an existing directory: \(request.output.path)")
            }
            // シンボリックリンク・ハードリンク・大文字小文字違いで同じ実体を指していないか。
            if sameFile(request.input, request.output, fileManager: fileManager) {
                throw DevelopError(.invalidRequest, "input and output refer to the same file")
            }
        }
        if request.input.resolvingSymlinksInPath().path == request.output.resolvingSymlinksInPath().path {
            throw DevelopError(.invalidRequest, "input and output refer to the same file")
        }
        if let dump = request.diagnostics?.dumpFloat {
            try validateDump(dump.path, input: request.input, output: request.output, fileManager: fileManager)
        }
    }

    static func sameFile(_ lhs: URL, _ rhs: URL, fileManager: FileManager) -> Bool {
        guard
            let a = try? fileManager.attributesOfItem(atPath: lhs.resolvingSymlinksInPath().path),
            let b = try? fileManager.attributesOfItem(atPath: rhs.resolvingSymlinksInPath().path),
            let aNode = a[.systemFileNumber] as? NSNumber, let bNode = b[.systemFileNumber] as? NSNumber,
            let aDev = a[.systemNumber] as? NSNumber, let bDev = b[.systemNumber] as? NSNumber
        else { return false }
        return aNode == bNode && aDev == bDev
    }
}
