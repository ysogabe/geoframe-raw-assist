import CoreImageDevelop
import Foundation

// stdout には JSON 1 行だけを書く。ログは stderr。

func emit(_ line: String) {
    FileHandle.standardOutput.write(Data((line + "\n").utf8))
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
