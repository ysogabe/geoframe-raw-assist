import CoreImageDevelop
import Foundation

// stdout には JSON 1 行だけを書く（`--batch` では応答 1 件につき 1 行）。ログは stderr。

func emit(_ line: String) {
    FileHandle.standardOutput.write(Data((line + "\n").utf8))
}

/// write(2) で 1 行を書き切る（stdout はパイプでもバッファしないので、書き終えた時点で読み手に届く）。
/// 失敗（EPIPE 等）なら false。`FileHandle.write` は失敗時に例外で落ちるのでバッチではこちらを使う。
func writeLineToStdout(_ line: String) -> Bool {
    let bytes = Array((line + "\n").utf8)
    var offset = 0
    while offset < bytes.count {
        let written = bytes[offset...].withUnsafeBytes { write(STDOUT_FILENO, $0.baseAddress, $0.count) }
        if written < 0 {
            if errno == EINTR { continue }
            return false
        }
        offset += written
    }
    return true
}

func log(_ message: String) {
    FileHandle.standardError.write(Data(("geoframe-ci-develop: " + message + "\n").utf8))
}

let arguments = Array(CommandLine.arguments.dropFirst())

if arguments.first == "--version" {
    emit(encodeJSONLine(VersionInfo()))
    exit(0)
}
if arguments.first == "--capabilities" {
    let probe = arguments.count > 1 ? URL(fileURLWithPath: arguments[1]) : nil
    emit(encodeJSONLine(Capabilities(probe: probe)))
    exit(0)
}
if arguments == ["--batch"] {
    // バッチモード（Issue #82）: stdin の JSON Lines を 1 行ずつ処理し、応答を 1 行ずつ書く。EOF で終了コード 0。
    // 読み手が先に居なくなっても SIGPIPE で落ちず、書き込み失敗（EPIPE）を見てそこで止める。
    signal(SIGPIPE, SIG_IGN)
    let processor = BatchProcessor(log: log)
    let count = processor.run(
        next: { readLine(strippingNewline: true) },
        emit: { writeLineToStdout($0) }
    )
    log("batch done: \(count) request(s)")
    exit(0)
}
if let unknown = arguments.first {
    emit(encodeJSONLine(FailureResponse(DevelopError(.invalidRequest, "unknown argument: \(unknown) (send the request as JSON on stdin)"))))
    exit(2)
}

let data = FileHandle.standardInput.readDataToEndOfFile()
do {
    let request = try RequestParser.parse(data)
    let response = try develop(request)
    log("ok \(response.mode) \(response.format) \(response.width)x\(response.height) total=\(response.elapsed_ms.total)ms")
    emit(encodeJSONLine(response))
    exit(0)
} catch let error as DevelopError {
    log("error \(error.code.rawValue): \(error.message)")
    emit(encodeJSONLine(FailureResponse(error)))
    exit(1)
} catch {
    log("internal error: \(error)")
    emit(encodeJSONLine(FailureResponse(DevelopError(.internalError, "\(error)"))))
    exit(1)
}
