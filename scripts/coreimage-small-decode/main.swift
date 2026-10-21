// Issue #102: Core Image の小サイズレンダ（PSO の代理画像、長辺 640px）の計測ハーネス。
//
// 本番の CLI には入れない実験コード。native/coreimage-cli/Sources/CoreImageDevelop の全ソースと一緒に
// 1 モジュールとして swiftc でコンパイルする（internal な applyProfile / applyAdjustmentsDecoder /
// PostChain をそのまま使い、本番と同じ設定でフィルタを組むため）。ビルドと実行は同じディレクトリの build.sh / run.sh。
//
//   bench timing --out DIR [--warmup W] [--iters N] ARW...   方式ごとの時間（JSON Lines）
//   bench sweep  --out DIR [--warmup W] [--iters N] ARW...   scaleFactor / draft の掃引
//   bench dump   --out DIR ARW...                           画質比較用の float32 / PNG / tiff16
//
// 計測条件は --batch と同じ: CIContext はプロセスで 1 つ（makeRenderContext()）、CIRAWFilter は毎回作る、
// 1 回ごとに clearCaches() と autoreleasepool。方式は 1 反復の中で交互に回す（熱・クロックの偏りを避ける）。
// ARW は読むだけ（CIRAWFilter / CGImageSource / Data(contentsOf:) で開くだけで書き込まない）。

import CoreImage
import Foundation
import ImageIO
import UniformTypeIdentifiers

let linearSpace = CGColorSpace(name: CGColorSpace.extendedLinearSRGB)!
let sRGBSpace = CGColorSpace(name: CGColorSpace.sRGB)!

func nowNS() -> UInt64 { DispatchTime.now().uptimeNanoseconds }
func ms(_ start: UInt64, _ end: UInt64) -> Double { (Double(end - start) / 1e5).rounded() / 10 }

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(("bench: " + message + "\n").utf8))
    exit(1)
}

func log(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

// MARK: - リクエスト

/// アプリの既定プリセット neutral_recovery（local_raw/presets.py）。PSO の基準点。
let presetAdjustments = Adjustments(
    exposure: 0, wbTemp: 0, shadowLift: 15, highlightRecovery: 60,
    brightness: 1.0, contrast: 1.03, saturation: 1.02, sharpness: 1.04, noiseReduction: .light
)

/// #85 が想定する代理レンダのリクエスト（640px・tiff16 sRGB・apple_default + レンズ補正・adjustments 全量）。
func makeRequest(_ input: URL, output: URL, format: String = "tiff16", adjustments: Adjustments? = presetAdjustments, maxEdge: Int = 640) -> DevelopRequest {
    var object: [String: Any] = [
        "input": input.path, "output": output.path, "mode": "preview", "max_edge": maxEdge,
        "format": format, "decoder_version": "9",
        "settings": ["profile": "apple_default", "lens_correction": true],
    ]
    if format == "tiff16" { object["output_color_space"] = "srgb" }
    let data = try! JSONSerialization.data(withJSONObject: object)
    var request: DevelopRequest
    do { request = try RequestParser.parse(data) } catch { fail("request: \(error)") }
    request.adjustments = adjustments
    return request
}

/// develop() の手順 1〜6 と同じ順でフィルタを組む（outputImage はまだ取らない）。
/// draft だけは develop() が false に固定しているので、ここで最後に上書きする。
func prepareFilter(_ request: DevelopRequest, draft: Bool = false, scale scaleOverride: Float? = nil, existing: CIRAWFilter? = nil) -> CIRAWFilter {
    guard let filter = existing ?? CIRAWFilter(imageURL: request.input) else { fail("CIRAWFilter failed: \(request.input.path)") }
    filter.decoderVersion = CIRAWDecoderVersion(rawValue: request.decoderVersion ?? "9")
    let native = filter.nativeSize
    let longEdge = max(native.width, native.height)
    var scale: Float = 1
    if let maxEdge = request.maxEdge { scale = min(1, Float(Double(maxEdge) / Double(longEdge))) }
    filter.scaleFactor = scaleOverride ?? scale
    filter.isDraftModeEnabled = false
    applyProfile(request.settings, to: filter)
    if let adjustments = request.adjustments {
        let defaults = readDefaults(filter)
        _ = applyAdjustmentsDecoder(adjustments, defaults: defaults, to: filter)
        applyOverrides(request.settings, to: filter)
        if WhiteDotGuard.needsGuard(
            luminanceNoiseReduction: filter.luminanceNoiseReductionAmount,
            sharpness: filter.sharpnessAmount, scaleFactor: filter.scaleFactor
        ) {
            filter.sharpnessAmount = max(filter.sharpnessAmount, WhiteDotGuard.sharpnessFloor)
        }
    } else {
        applyOverrides(request.settings, to: filter)
    }
    if draft { filter.isDraftModeEnabled = true }
    return filter
}

/// develop() と同じ後段パラメータ（USM の長辺・ISO は decoded と filter から）。
func postParameters(_ adjustments: Adjustments, decodedExtent: CGRect, iso: Int?) -> PostChainParameters {
    let integral = decodedExtent.integral
    let longEdge = Int(max(integral.width, integral.height))
    let base = captureSharpenBase(longEdge: longEdge, iso: iso.map(Double.init))
    return PostChainParameters(
        lift: adjustments.shadowLift / 100.0, brightness: adjustments.brightness, contrast: adjustments.contrast,
        saturation: adjustments.saturation, usmSigma: base.sigma,
        usmGain: usmGainForCoreImage(sharpness: adjustments.sharpness, base: base), usmCoring: base.coring
    )
}

// MARK: - レンダ

struct Bitmap {
    let data: Data
    let width: Int
    let height: Int
    let rowBytes: Int
    let format: CIFormat
    let bytesPerPixel: Int
}

/// image を extent 全体で bitmap へレンダする（同期。GPU の完了と読み出しまで含む）。
func renderBitmap(_ image: CIImage, context: CIContext, format: CIFormat = .RGBAh, colorSpace: CGColorSpace = linearSpace) -> Bitmap {
    let extent = image.extent.integral
    let width = Int(extent.width), height = Int(extent.height)
    let bpp: Int
    switch format {
    case .RGBAf: bpp = 16
    case .RGBAh, .RGBA16: bpp = 8
    default: bpp = 4
    }
    let rowBytes = width * bpp
    var data = Data(count: rowBytes * height)
    data.withUnsafeMutableBytes { raw in
        context.render(image, toBitmap: raw.baseAddress!, rowBytes: rowBytes, bounds: extent, format: format, colorSpace: colorSpace)
    }
    return Bitmap(data: data, width: width, height: height, rowBytes: rowBytes, format: format, bytesPerPixel: bpp)
}

/// キャッシュしたビットマップを CIImage に戻す（色空間は書き出しと同じ extended linear sRGB。変換は起きない）。
func ciImage(_ bitmap: Bitmap) -> CIImage {
    CIImage(bitmapData: bitmap.data, bytesPerRow: bitmap.rowBytes, size: CGSize(width: bitmap.width, height: bitmap.height),
            format: bitmap.format, colorSpace: linearSpace)
}

/// 線形空間でのゲイン（exposure を後段で近似する: 2^EV 倍）。
func linearGain(_ image: CIImage, _ gain: (r: Double, g: Double, b: Double)) -> CIImage {
    image.applyingFilter("CIColorMatrix", parameters: [
        "inputRVector": CIVector(x: gain.r, y: 0, z: 0, w: 0),
        "inputGVector": CIVector(x: 0, y: gain.g, z: 0, w: 0),
        "inputBVector": CIVector(x: 0, y: 0, z: gain.b, w: 0),
        "inputAVector": CIVector(x: 0, y: 0, z: 0, w: 1),
        "inputBiasVector": CIVector(x: 0, y: 0, z: 0, w: 0),
    ])
}

/// 埋め込みプレビュー（ImageIO のサムネイル経路）。長辺 maxPixel に縮小、向き適用。
func imageIOThumbnail(_ url: URL, maxPixel: Int) -> CGImage? {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
    let options: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageIfAbsent: true,
        kCGImageSourceThumbnailMaxPixelSize: maxPixel,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceShouldCacheImmediately: true,
    ]
    return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
}

/// CIRAWFilter.previewImage を長辺 maxPixel へ縮小した CIImage（previewImage が無ければ nil）。
func ciPreview(_ url: URL, maxPixel: Int) -> CIImage? {
    guard let filter = CIRAWFilter(imageURL: url), let preview = filter.previewImage else { return nil }
    let extent = preview.extent
    let scale = Double(maxPixel) / Double(max(extent.width, extent.height))
    let scaled = preview.applyingFilter("CILanczosScaleTransform", parameters: [kCIInputScaleKey: scale, kCIInputAspectRatioKey: 1.0])
    return scaled.cropped(to: scaled.extent.integral)
}

func forceDecode(_ image: CGImage) -> Int {
    (image.dataProvider?.data as Data?)?.count ?? 0
}

func writePNG(_ image: CGImage, to url: URL) {
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else { fail("png dest") }
    CGImageDestinationAddImage(destination, image, nil)
    if !CGImageDestinationFinalize(destination) { fail("png write \(url.path)") }
}

/// RGBAf ビットマップを生の float32（RGBA, little endian）で書く。寸法は meta.json に載せる。
func writeFloat(_ bitmap: Bitmap, to url: URL) {
    precondition(bitmap.format == .RGBAf)
    try! bitmap.data.write(to: url)
}

// MARK: - 引数

var argv = Array(CommandLine.arguments.dropFirst())
guard let command = argv.first else { fail("usage: bench timing|sweep|dump --out DIR [--warmup W] [--iters N] ARW...") }
argv.removeFirst()
var outDir: URL?
var localDir: URL?
var warmup = 2
var iters = 10
var inputs: [URL] = []
while !argv.isEmpty {
    let arg = argv.removeFirst()
    switch arg {
    case "--out": outDir = URL(fileURLWithPath: argv.removeFirst())
    case "--warmup": warmup = Int(argv.removeFirst())!
    case "--iters": iters = Int(argv.removeFirst())!
    case "--local": localDir = URL(fileURLWithPath: argv.removeFirst())
    default: inputs.append(URL(fileURLWithPath: arg))
    }
}
guard let out = outDir, !inputs.isEmpty else { fail("--out and ARW files are required") }
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

let context = makeRenderContext()
var lines: [String] = []

func record(_ fields: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
    let line = String(decoding: data, as: UTF8.self)
    lines.append(line)
    print(line)
    fflush(stdout)
}

/// 1 回分を autoreleasepool + clearCaches で囲んで実行する（--batch と同じ）。
func measured(_ body: () -> [String: Any]) -> [String: Any] {
    autoreleasepool {
        defer { context.clearCaches() }
        return body()
    }
}

func isoOf(_ url: URL) -> Int? {
    guard let filter = CIRAWFilter(imageURL: url) else { return nil }
    return isoSpeed(from: filter.properties)
}

// MARK: - timing

/// PSO の 1 評価で変わる後段パラメータ（キャッシュ方式で毎回違う値を回すため）。
let postVariants: [Adjustments] = [
    presetAdjustments,
    Adjustments(exposure: 0, wbTemp: 0, shadowLift: 35, highlightRecovery: 60, brightness: 1.08, contrast: 1.1, saturation: 1.1, sharpness: 1.3, noiseReduction: .light),
    Adjustments(exposure: 0, wbTemp: 0, shadowLift: 5, highlightRecovery: 60, brightness: 0.95, contrast: 0.95, saturation: 0.9, sharpness: 1.0, noiseReduction: .light),
    Adjustments(exposure: 0, wbTemp: 0, shadowLift: 25, highlightRecovery: 60, brightness: 1.2, contrast: 1.25, saturation: 1.3, sharpness: 1.45, noiseReduction: .light),
]

func runTiming() {
    struct Sample {
        let url: URL
        let iso: Int?
        let cacheH: Bitmap
        let cacheF: Bitmap
        let cacheBuildMS: Double
        let cgForEncode: CGImage
    }
    var samples: [Sample] = []
    for url in inputs {
        _ = try? Data(contentsOf: url)  // ページキャッシュに載せる
        let request = makeRequest(url, output: out.appendingPathComponent("x.tiff"))
        // キャッシュの作成（= デコード 1 回）。計測は timing のループ側の D_cache_build で取る。
        var buildMS = 0.0
        let (h, f, cg) = autoreleasepool { () -> (Bitmap, Bitmap, CGImage) in
            defer { context.clearCaches() }
            let t0 = nowNS()
            let filter = prepareFilter(request)
            let decoded = filter.outputImage!
            let h = renderBitmap(decoded, context: context, format: .RGBAh)
            buildMS = ms(t0, nowNS())
            let f = renderBitmap(decoded, context: context, format: .RGBAf)
            let cg = try! renderRGB16(decoded, extent: decoded.extent.integral, colorSpace: sRGBSpace, context: context)
            return (h, f, cg)
        }
        samples.append(Sample(url: url, iso: isoOf(url), cacheH: h, cacheF: f, cacheBuildMS: buildMS, cgForEncode: cg))
        log("prepared \(url.lastPathComponent) \(h.width)x\(h.height) cache_build=\(buildMS)ms")
    }

    typealias Variant = (name: String, run: (Sample, Int) -> [String: Any])
    let variants: [Variant] = [
        ("io_read", { s, _ in
            let t0 = nowNS(); let data = try! Data(contentsOf: s.url); return ["total": ms(t0, nowNS()), "bytes": data.count]
        }),
        ("A_develop_tiff16_adj", { s, _ in
            // #85 の代理レンダそのもの（CLI の 1 件と同じ develop()）。
            let request = makeRequest(s.url, output: out.appendingPathComponent("A_\(s.url.deletingPathExtension().lastPathComponent).tiff"))
            let t0 = nowNS(); let response = try! develop(request, context: context); let total = ms(t0, nowNS())
            return ["total": total, "cli_decode": response.elapsed_ms.decode, "cli_render_encode": response.elapsed_ms.render_encode, "w": response.width, "h": response.height]
        }),
        ("A_develop_jpeg_noadj", { s, _ in
            // #82 の計測と同じ条件（adjustments 無し・jpeg）。
            let request = makeRequest(s.url, output: out.appendingPathComponent("A_\(s.url.deletingPathExtension().lastPathComponent).jpg"), format: "jpeg", adjustments: nil)
            let t0 = nowNS(); let response = try! develop(request, context: context); let total = ms(t0, nowNS())
            return ["total": total, "cli_decode": response.elapsed_ms.decode, "cli_render_encode": response.elapsed_ms.render_encode]
        }),
        ("B_decode_lens_off", { s, _ in stageRun(s.url, lens: false) }),
        ("B_decode_lens_on", { s, _ in stageRun(s.url, lens: true) }),
        ("B_decode_lens_on_nr0_sharp0", { s, _ in stageRun(s.url, lens: true, nrSharpOff: true) }),
        ("B_decode_lens_on_draft", { s, _ in stageRun(s.url, lens: true, draft: true) }),
        ("B_decode_lens_on_post", { s, i in stageRun(s.url, lens: true, post: postVariants[i % postVariants.count]) }),
        ("E_encode_tiff16", { s, _ in
            let t0 = nowNS()
            try! AtomicImageWriter.write(s.cgForEncode, to: out.appendingPathComponent("E.tiff"), format: .tiff16, jpegQuality: nil)
            return ["total": ms(t0, nowNS())]
        }),
        ("E_encode_jpeg", { s, _ in
            let t0 = nowNS()
            try! AtomicImageWriter.write(s.cgForEncode, to: out.appendingPathComponent("E.jpg"), format: .jpeg, jpegQuality: 0.92)
            return ["total": ms(t0, nowNS())]
        }),
        ("C_thumb_imageio", { s, _ in
            let t0 = nowNS(); let thumb = imageIOThumbnail(s.url, maxPixel: 640); let bytes = thumb.map(forceDecode) ?? -1
            return ["total": ms(t0, nowNS()), "w": thumb?.width ?? -1, "h": thumb?.height ?? -1, "bytes": bytes]
        }),
        ("C_preview_ciraw", { s, _ in
            let t0 = nowNS()
            guard let preview = ciPreview(s.url, maxPixel: 640) else { return ["total": -1] }
            let tGraph = nowNS()
            let bitmap = renderBitmap(preview, context: context, format: .RGBAh)
            return ["total": ms(t0, nowNS()), "graph": ms(t0, tGraph), "w": bitmap.width, "h": bitmap.height]
        }),
        ("D_cache_build", { s, _ in
            // キャッシュ方式の初回（デコード 1 回 + RGBAh への読み出し）。
            let request = makeRequest(s.url, output: out.appendingPathComponent("x.tiff"))
            let t0 = nowNS(); let filter = prepareFilter(request); let decoded = filter.outputImage!
            let bitmap = renderBitmap(decoded, context: context, format: .RGBAh)
            return ["total": ms(t0, nowNS()), "w": bitmap.width, "h": bitmap.height]
        }),
        ("D_post_from_cacheH_rgb16", { s, i in
            // キャッシュ（RGBAh）→ 後段 → RGB16 sRGB の CGImage（PSO が画素を受け取る形）。
            let adj = postVariants[i % postVariants.count]
            let t0 = nowNS()
            let image = ciImage(s.cacheH)
            let params = postParameters(adj, decodedExtent: image.extent, iso: s.iso)
            let processed = try! PostChain.apply(image, params)
            let cg = try! renderRGB16(processed, extent: processed.extent.integral, colorSpace: sRGBSpace, context: context)
            return ["total": ms(t0, nowNS()), "w": cg.width, "h": cg.height]
        }),
        ("D_post_from_cacheF_rgb16", { s, i in
            let adj = postVariants[i % postVariants.count]
            let t0 = nowNS()
            let image = ciImage(s.cacheF)
            let params = postParameters(adj, decodedExtent: image.extent, iso: s.iso)
            let processed = try! PostChain.apply(image, params)
            _ = try! renderRGB16(processed, extent: processed.extent.integral, colorSpace: sRGBSpace, context: context)
            return ["total": ms(t0, nowNS())]
        }),
        ("D_post_from_cacheH_gain_tiff16_write", { s, i in
            // 後段 + exposure の線形ゲイン近似 + tiff16 書き出し（CLI のバッチ 1 行の応答相当）。
            let adj = postVariants[i % postVariants.count]
            let ev = [0.25, -0.25, 0.4, -0.1][i % 4]
            let t0 = nowNS()
            let gain = pow(2.0, ev)
            let image = linearGain(ciImage(s.cacheH), (gain, gain, gain))
            let params = postParameters(adj, decodedExtent: image.extent, iso: s.iso)
            let processed = try! PostChain.apply(image, params)
            let cg = try! renderRGB16(processed, extent: processed.extent.integral, colorSpace: sRGBSpace, context: context)
            try! AtomicImageWriter.write(cg, to: out.appendingPathComponent("D.tiff"), format: .tiff16, jpegQuality: nil)
            return ["total": ms(t0, nowNS())]
        }),
    ]

    for iteration in 0..<(warmup + iters) {
        for sample in samples {
            for variant in variants {
                let result = measured { variant.run(sample, iteration) }
                var fields = result
                fields["sample"] = sample.url.deletingPathExtension().lastPathComponent
                fields["variant"] = variant.name
                fields["iter"] = iteration
                fields["warm"] = iteration < warmup
                record(fields)
            }
        }
        log("iteration \(iteration + 1)/\(warmup + iters) done")
    }
}

/// B 系: フィルタ生成〜outputImage〜（任意で後段）〜RGBAh レンダを 1 回。段ごとの時刻を返す。
func stageRun(_ url: URL, lens: Bool, draft: Bool = false, nrSharpOff: Bool = false, post: Adjustments? = nil, scale: Float? = nil,
              open: (() -> CIRAWFilter?)? = nil) -> [String: Any] {
    var request = makeRequest(url, output: out.appendingPathComponent("x.tiff"), adjustments: post ?? presetAdjustments)
    request.settings.lensCorrection = lens
    if nrSharpOff {
        request.settings.luminanceNoiseReduction = 0
        request.settings.colorNoiseReduction = 0
        request.settings.sharpnessAmount = 0  // adjustments 指定時は白点ガードで 0.1 に床上げされる
    }
    let t0 = nowNS()
    guard let filter = open?() ?? CIRAWFilter(imageURL: url) else { fail("open") }
    let tOpen = nowNS()
    let configured = prepareFilter(request, draft: draft, scale: scale, existing: filter)
    let tConfigured = nowNS()
    var image = configured.outputImage!
    let decodedExtent = image.extent
    if let post {
        image = try! PostChain.apply(image, postParameters(post, decodedExtent: decodedExtent, iso: isoSpeed(from: configured.properties)))
    }
    let tGraph = nowNS()
    let bitmap = renderBitmap(image, context: context, format: .RGBAh)
    let tRender = nowNS()
    return [
        "total": ms(t0, tRender),
        "create": ms(t0, tOpen),
        "create_configure": ms(tOpen, tConfigured),
        "graph": ms(tConfigured, tGraph),
        "render": ms(tGraph, tRender),
        "w": bitmap.width, "h": bitmap.height,
        "draft_readback": configured.isDraftModeEnabled,
        "scale_readback": Double(configured.scaleFactor),
    ]
}

// MARK: - io（ARW の置き場所と読み方の影響）

/// SMB 上の URL・ローカルコピーの URL・メモリ上の Data（CIRAWFilter(imageData:identifierHint:)）で
/// 同じデコード（640px・レンズ補正・RGBAh レンダ）を比べる。--local にローカルコピーのディレクトリを渡す。
func runIO() {
    guard let localDir else { fail("io needs --local DIR") }
    var memory: [URL: Data] = [:]
    for url in inputs {
        let local = localDir.appendingPathComponent(url.lastPathComponent)
        memory[url] = try! Data(contentsOf: local)
    }
    for iteration in 0..<(warmup + iters) {
        for url in inputs {
            let local = localDir.appendingPathComponent(url.lastPathComponent)
            let data = memory[url]!
            let variants: [(String, () -> [String: Any])] = [
                ("dec_url_smb", { stageRun(url, lens: true) }),
                ("dec_url_local", { stageRun(local, lens: true) }),
                ("dec_data_memory", { stageRun(url, lens: true, open: { CIRAWFilter(imageData: data, identifierHint: "com.sony.arw-raw-image") }) }),
                ("io_read_local", { let t0 = nowNS(); let d = try! Data(contentsOf: local); return ["total": ms(t0, nowNS()), "bytes": d.count] }),
            ]
            for (name, run) in variants {
                var fields = measured(run)
                fields["sample"] = url.deletingPathExtension().lastPathComponent
                fields["variant"] = name
                fields["iter"] = iteration
                fields["warm"] = iteration < warmup
                record(fields)
            }
        }
        log("io iteration \(iteration + 1)/\(warmup + iters) done")
    }
    // SMB の全量読み込みは最後に（先に読むと SMB クライアントのキャッシュで dec_url_smb が速く見える）。
    for url in inputs {
        let t0 = nowNS(); let d = try! Data(contentsOf: url)
        record(["sample": url.deletingPathExtension().lastPathComponent, "variant": "io_read_smb_once", "total": ms(t0, nowNS()), "bytes": d.count, "iter": 0, "warm": false])
    }
}

// MARK: - sweep

func runSweep() {
    let scales: [(String, Float?, Bool)] = [
        ("s1.000", 1.0, false), ("s1.000_draft", 1.0, true),
        ("s0.500", 0.5, false), ("s0.500_draft", 0.5, true),
        ("s0.250", 0.25, false),
        ("s0.107(640)", nil, false), ("s0.107(640)_draft", nil, true),
        ("s0.050", 0.05, false), ("s0.020", 0.02, false),
    ]
    for url in inputs { _ = try? Data(contentsOf: url) }
    for iteration in 0..<(warmup + iters) {
        for url in inputs {
            for (name, scale, draft) in scales {
                var fields = measured { stageRun(url, lens: true, draft: draft, scale: scale) }
                fields["sample"] = url.deletingPathExtension().lastPathComponent
                fields["variant"] = name
                fields["iter"] = iteration
                fields["warm"] = iteration < warmup
                record(fields)
            }
        }
        log("sweep iteration \(iteration + 1)/\(warmup + iters) done")
    }
}

// MARK: - dump（画質比較用）

func runDump() {
    for url in inputs {
        let name = url.deletingPathExtension().lastPathComponent
        let dir = out.appendingPathComponent(name)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var meta: [String: Any] = ["sample": name]
        let iso = isoOf(url)
        meta["iso"] = iso ?? -1

        // デコード段の出力（後段なし）を RGBAf（extended linear sRGB）で書く。
        func dumpDecoded(_ key: String, adjustments: Adjustments = presetAdjustments, lens: Bool = true, draft: Bool = false, fromData: Bool = false) {
            autoreleasepool {
                defer { context.clearCaches() }
                var request = makeRequest(url, output: dir.appendingPathComponent("x.tiff"), adjustments: adjustments)
                request.settings.lensCorrection = lens
                let existing = fromData ? CIRAWFilter(imageData: try! Data(contentsOf: url), identifierHint: "com.sony.arw-raw-image") : nil
                let filter = prepareFilter(request, draft: draft, existing: existing)
                let image = filter.outputImage!
                let bitmap = renderBitmap(image, context: context, format: .RGBAf)
                writeFloat(bitmap, to: dir.appendingPathComponent("\(key).f32"))
                meta[key] = [
                    "w": bitmap.width, "h": bitmap.height,
                    "draft_readback": filter.isDraftModeEnabled,
                    "sharpness_amount": Double(filter.sharpnessAmount),
                    "lnr": Double(filter.luminanceNoiseReductionAmount), "cnr": Double(filter.colorNoiseReductionAmount),
                    "neutral_temperature": Double(filter.neutralTemperature), "exposure": Double(filter.exposure),
                ]
            }
        }
        dumpDecoded("dec_ref")
        dumpDecoded("dec_ref_from_data", fromData: true)
        for ev in [-2.0, -1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0] {
            var adj = presetAdjustments; adj.exposure = ev
            dumpDecoded(String(format: "dec_ev%+.2f", ev), adjustments: adj)
        }
        for wb in [-50.0, -20.0, 20.0, 50.0] {
            var adj = presetAdjustments; adj.wbTemp = wb
            dumpDecoded(String(format: "dec_wb%+.0f", wb), adjustments: adj)
        }
        for s in [0.8, 0.9] {
            var adj = presetAdjustments; adj.sharpness = s
            dumpDecoded(String(format: "dec_sharp%.2f", s), adjustments: adj)
        }
        for nr in [NoiseReduction.off, .full] {
            var adj = presetAdjustments; adj.noiseReduction = nr
            dumpDecoded("dec_nr_\(nr.rawValue)", adjustments: adj)
        }
        dumpDecoded("dec_draft", draft: true)
        dumpDecoded("dec_lens_off", lens: false)

        // 後段まで: 本番 develop()（tiff16 sRGB）とキャッシュ経路（RGBAh / RGBAf → 後段 → tiff16）の一致確認。
        for (key, adj) in [("post_preset", presetAdjustments), ("post_strong", postVariants[3])] {
            autoreleasepool {
                defer { context.clearCaches() }
                let request = makeRequest(url, output: dir.appendingPathComponent("\(key)_develop.tiff"), adjustments: adj)
                _ = try! develop(request, context: context)
            }
            for format in [CIFormat.RGBAh, .RGBAf] {
                autoreleasepool {
                    defer { context.clearCaches() }
                    let request = makeRequest(url, output: dir.appendingPathComponent("x.tiff"), adjustments: adj)
                    let filter = prepareFilter(request)
                    let decoded = filter.outputImage!
                    let cache = renderBitmap(decoded, context: context, format: format)
                    context.clearCaches()
                    let image = ciImage(cache)
                    let processed = try! PostChain.apply(image, postParameters(adj, decodedExtent: image.extent, iso: iso))
                    let cg = try! renderRGB16(processed, extent: processed.extent.integral, colorSpace: sRGBSpace, context: context)
                    let suffix = format == .RGBAh ? "cacheH" : "cacheF"
                    try! AtomicImageWriter.write(cg, to: dir.appendingPathComponent("\(key)_\(suffix).tiff"), format: .tiff16, jpegQuality: nil)
                }
            }
        }
        // exposure を後段のゲインで近似したときの、後段（preset）まで通した画（ΔE を後段込みでも見る）。
        for ev in [-1.0, -0.5, -0.25, 0.25, 0.5, 1.0] {
            var adj = presetAdjustments; adj.exposure = ev
            autoreleasepool {
                defer { context.clearCaches() }
                let request = makeRequest(url, output: dir.appendingPathComponent(String(format: "postev%+.2f_develop.tiff", ev)), adjustments: adj)
                _ = try! develop(request, context: context)
            }
            autoreleasepool {
                defer { context.clearCaches() }
                let request = makeRequest(url, output: dir.appendingPathComponent("x.tiff"))
                let filter = prepareFilter(request)
                let cache = renderBitmap(filter.outputImage!, context: context, format: .RGBAh)
                context.clearCaches()
                let gain = pow(2.0, ev)
                let image = linearGain(ciImage(cache), (gain, gain, gain))
                let processed = try! PostChain.apply(image, postParameters(presetAdjustments, decodedExtent: image.extent, iso: iso))
                let cg = try! renderRGB16(processed, extent: processed.extent.integral, colorSpace: sRGBSpace, context: context)
                try! AtomicImageWriter.write(cg, to: dir.appendingPathComponent(String(format: "postev%+.2f_gain.tiff", ev)), format: .tiff16, jpegQuality: nil)
            }
        }

        // 埋め込みプレビュー。
        if let source = CGImageSourceCreateWithURL(url as CFURL, nil) {
            var images: [[String: Any]] = []
            for index in 0..<CGImageSourceGetCount(source) {
                let p = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [String: Any] ?? [:]
                images.append(["index": index, "w": p[kCGImagePropertyPixelWidth as String] ?? -1, "h": p[kCGImagePropertyPixelHeight as String] ?? -1])
            }
            meta["imagesource_images"] = images
            if let props = CGImageSourceCopyProperties(source, nil) as? [String: Any] {
                // ファイル全体のプロパティから GPS は読まない・書かない（寸法の手がかりだけ）。
                meta["imagesource_has_thumbnail_dicts"] = (props[kCGImagePropertyFileContentsDictionary as String] != nil)
            }
        }
        if let thumb = imageIOThumbnail(url, maxPixel: 640) {
            writePNG(thumb, to: dir.appendingPathComponent("preview_imageio.png"))
            meta["preview_imageio"] = ["w": thumb.width, "h": thumb.height]
        }
        if let filter = CIRAWFilter(imageURL: url), let preview = filter.previewImage {
            meta["preview_ciraw_native"] = ["w": Double(preview.extent.width), "h": Double(preview.extent.height)]
        }
        if let preview = ciPreview(url, maxPixel: 640),
           let cg = context.createCGImage(preview, from: preview.extent.integral, format: .RGBA8, colorSpace: sRGBSpace) {
            writePNG(cg, to: dir.appendingPathComponent("preview_ciraw.png"))
            meta["preview_ciraw"] = ["w": cg.width, "h": cg.height]
        }
        let data = try! JSONSerialization.data(withJSONObject: meta, options: [.sortedKeys, .prettyPrinted])
        try! data.write(to: dir.appendingPathComponent("meta.json"))
        log("dumped \(name)")
    }
}

switch command {
case "timing": runTiming()
case "sweep": runSweep()
case "dump": runDump()
case "io": runIO()
default: fail("unknown command \(command)")
}
let joined = lines.joined(separator: "\n") + (lines.isEmpty ? "" : "\n")
try! joined.write(to: out.appendingPathComponent("\(command).jsonl"), atomically: true, encoding: .utf8)
