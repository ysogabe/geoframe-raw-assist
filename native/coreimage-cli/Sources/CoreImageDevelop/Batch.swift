import CoreImage
import Foundation

/// `--batch` モード（CLI 0.5.0、Issue #82）の 1 行ごとの処理。
///
/// - 1 行 = 1 リクエスト（単発と同じ JSON）。空行（空白だけの行を含む）は読み飛ばし、番号も進めない。
/// - 空行以外は、JSON として壊れていても 1 件として番号（`sequence`、1 始まり）を振り、必ず応答 1 行を返す。
/// - 1 件が失敗しても `{"ok":false,...}` を返して次へ進む（例外を外へ出さない）。
/// - `CIContext` はプロセスで 1 つを使い回し、1 件ごとに `clearCaches()` する。`CIRAWFilter` は毎回作る
///   （使い回さない理由と計測は README「バッチモード」）。
public final class BatchProcessor {
    public let context: CIContext
    /// 直前に振った番号（まだ 1 件も受けていなければ 0）。
    public private(set) var sequence = 0
    private let log: (String) -> Void

    public init(context: CIContext = makeRenderContext(), log: @escaping (String) -> Void = { _ in }) {
        self.context = context
        self.log = log
    }

    /// 1 行を処理して応答 1 行（改行なし）を返す。空行なら nil（応答を書かない）。
    public func handle(line: String) -> String? {
        if line.allSatisfy({ $0.isWhitespace }) {
            return nil
        }
        sequence += 1
        let current = sequence
        // 1 件ごとに autorelease pool を空にする（Core Image / ImageIO の一時オブジェクトを件数ぶん溜めない）。
        return autoreleasepool {
            do {
                let request = try RequestParser.parse(Data(line.utf8))
                // 使い回す CIContext のキャッシュは 1 件ごとに捨てる。捨てないと同じ RAW を続けて回したときに
                // 1 件あたりの時間が伸び続けた（30 件で 1.3 秒 → 2.3 秒。README「バッチモード」の計測）。
                defer { context.clearCaches() }
                var response = try develop(request, context: context)
                response.sequence = current
                log("#\(current) ok \(response.mode) \(response.format) \(response.width)x\(response.height) total=\(response.elapsed_ms.total)ms")
                return encodeJSONLine(response)
            } catch let error as DevelopError {
                log("#\(current) error \(error.code.rawValue): \(error.message)")
                return encodeJSONLine(FailureResponse(error, sequence: current))
            } catch {
                log("#\(current) internal error: \(error)")
                return encodeJSONLine(FailureResponse(DevelopError(.internalError, "\(error)"), sequence: current))
            }
        }
    }

    /// `next` が nil（EOF）を返すまで 1 行ずつ処理し、応答を `emit` へ渡す。`emit` が false を返したら
    /// （stdout が閉じられた等）そこで止める。処理した件数（空行を除く）を返す。
    @discardableResult
    public func run(next: () -> String?, emit: (String) -> Bool) -> Int {
        while let line = next() {
            if let response = handle(line: line), !emit(response) {
                break
            }
        }
        return sequence
    }
}
