import Foundation

/// CLI のバージョン。応答 JSON の `cli_version` に載る。
public let cliVersion = "0.1.0"

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
    /// nil = そのファイルで使える最新の decoderVersion。
    public var decoderVersion: String?
    public var settings: Settings
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

    static let topLevelKeys: Set<String> = [
        "input", "output", "mode", "max_edge", "format", "jpeg_quality", "decoder_version", "settings",
    ]
    static let settingsKeys: Set<String> = [
        "exposure_ev", "neutral_temperature", "neutral_tint", "lens_correction", "profile",
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

        var decoderVersion: String?
        if let raw = dict["decoder_version"], !(raw is NSNull) {
            guard let string = raw as? String, allowedDecoderVersions.contains(string) else {
                throw DevelopError(.invalidRequest, "decoder_version must be \"8\" or \"9\" (or omitted for the latest)")
            }
            decoderVersion = string
        }

        let settings = try parseSettings(dict["settings"])

        guard input.pathExtension.lowercased() == "arw" else {
            throw DevelopError(.invalidRequest, "input must be a Sony .ARW file")
        }
        if input.standardizedFileURL.path == output.standardizedFileURL.path {
            throw DevelopError(.invalidRequest, "input and output must be different paths")
        }

        return DevelopRequest(
            input: input, output: output, mode: mode, maxEdge: maxEdge, format: format,
            jpegQuality: jpegQuality, decoderVersion: decoderVersion, settings: settings
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
        return settings
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
