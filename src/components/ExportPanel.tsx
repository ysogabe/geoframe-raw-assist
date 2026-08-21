import type { PhotoExportVariant } from "../lib/app-state";
import { isValidBatchName } from "../lib/app-state";
import { rawPillClass } from "../lib/format";
import { assetUrl } from "../lib/raw-api";
import type {
  Analysis,
  CaptureId,
  DevelopedResult,
  ExportResponse,
  GroupInfo,
  ImportToPhotosResponse,
  MergedResult,
} from "../lib/types";

/**
 * 書き出し 1 行。採用アイテム 1 件に 1 行が対応する（採用順のまま）。
 *
 * 成果物（developed / merged）は **採用時のスナップショットではなく** 書き出し画面を
 * 開いた時点で引き直した結果が入る。本現像をやり直したらこの行も新しい結果を指す。
 */
export type ExportRow =
  | {
      kind: "photo";
      /** acceptedKey。React の key と、形式変更の宛先に使う。 */
      key: string;
      group: GroupInfo;
      captureId: CaptureId;
      developed: DevelopedResult | undefined;
      variant: PhotoExportVariant;
    }
  | {
      kind: "merged";
      key: string;
      group: GroupInfo;
      /** 合成の先頭フレーム。サムネと POST /api/export の capture_id に使う。 */
      captureId: CaptureId;
      /**
       * 採用した合成の本合成結果。undefined なら成果物が失われている
       * （先頭フレームの違う合成でやり直した等）。行は消さずに理由を出す。
       */
      merged: MergedResult | undefined;
    };

type ExportPanelProps = {
  analysis: Analysis;
  rows: ExportRow[];
  batchName: string;
  onChangeBatchName: (value: string) => void;
  onChangeVariant: (key: string, variant: PhotoExportVariant) => void;
  onExport: () => void;
  onImport: () => void;
  exporting: boolean;
  importing: boolean;
  exportResult: ExportResponse | null;
  importResult: ImportToPhotosResponse | null;
  albumName: string;
  onBack: () => void;
};

export function ExportPanel({
  analysis,
  rows,
  batchName,
  onChangeBatchName,
  onChangeVariant,
  onExport,
  onImport,
  exporting,
  importing,
  exportResult,
  importResult,
  albumName,
  onBack,
}: ExportPanelProps) {
  const nameValid = isValidBatchName(batchName);
  const canExport = rows.length > 0 && nameValid && !exporting;

  return (
    <section className="export-screen screen-enter" aria-label="書き出し">
      <div className="section-heading">
        <div><p className="eyebrow">EXPORT</p><h2>採用した写真を書き出す</h2></div>
        <button className="page-back" onClick={onBack}><span>←</span> おすすめ一覧</button>
      </div>

      {rows.length === 0 ? (
        <div className="empty-state">
          <span>○</span>
          <h3>採用した写真がありません</h3>
          <p>おすすめ一覧から操作パネルを開き、「この写真を採用」を押してください。</p>
        </div>
      ) : (
        <div className="export-card">
          <ul className="export-list">
            {rows.map((row) => {
              const photo = analysis.photos[row.captureId];
              return (
                <li key={row.key} className="export-row">
                  <div className="export-thumb">
                    {photo ? <img src={assetUrl(photo.thumb)} alt={row.captureId} loading="lazy" decoding="async" /> : null}
                  </div>
                  <div className="export-info">
                    {/* この画面の行は全部が採用アイテムだが、バッジはタイル・一覧カードと
                        同じ見た目で出しておく（「採用」の印が画面ごとに違うと読み替えが要る）。 */}
                    <span className="export-title">
                      <strong>{row.captureId}</strong>
                      <span className="accepted-badge inline">✓ 採用</span>
                    </span>
                    <small>{row.group.title}</small>
                    <span className={rawPillClass(row.group.raw.level)}>{row.group.raw.label}</span>
                  </div>
                  {/* 合成の採用行には形式の選択肢が無い（合成は「写真の別形式」ではない）。 */}
                  {row.kind === "merged" ? (
                    <div className="export-variant">
                      {row.merged ? (
                        <span className="merged-tag ok" title={`${row.merged.filename}（${row.merged.frames}枚合成）`}>
                          合成 {row.merged.frames}枚
                        </span>
                      ) : (
                        <span className="merged-tag" title="採用した合成の結果が見つかりません。合成し直して採用し直してください。">
                          合成結果なし
                        </span>
                      )}
                    </div>
                  ) : (
                    <div className="export-variant" role="radiogroup" aria-label={`${row.captureId} の書き出し形式`}>
                      <button
                        className={row.variant === "camera_jpg" ? "active" : ""}
                        role="radio"
                        aria-checked={row.variant === "camera_jpg"}
                        onClick={() => onChangeVariant(row.key, "camera_jpg")}
                      >
                        カメラJPG
                      </button>
                      <button
                        className={row.variant === "developed" ? "active" : ""}
                        role="radio"
                        aria-checked={row.variant === "developed"}
                        disabled={!row.developed}
                        title={row.developed ? row.developed.filename : "本現像の結果がありません"}
                        onClick={() => onChangeVariant(row.key, "developed")}
                      >
                        現像済み
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>

          <div className="export-form">
            <label htmlFor="batch-name">バッチ名</label>
            <input
              id="batch-name"
              value={batchName}
              placeholder="travel_2025_06"
              onChange={(event) => onChangeBatchName(event.target.value)}
              aria-invalid={batchName.length > 0 && !nameValid}
            />
            <button className="primary-action" onClick={onExport} disabled={!canExport}>
              {exporting ? "書き出し中…" : "書き出し"}
            </button>
          </div>
          <p className={`export-hint${batchName.length > 0 && !nameValid ? " invalid" : ""}`}>
            半角英数字・ハイフン・アンダースコアのみ使えます。<code>outputs/export/{nameValid ? batchName : "<バッチ名>"}/</code> にコピーします。
          </p>
        </div>
      )}

      {exportResult && (
        <div className="export-result">
          <div className="export-result-head">
            <span className="comparison-check">✓</span>
            <strong>{exportResult.files.length}件を書き出しました</strong>
          </div>
          <p className="export-dir">{exportResult.export_dir}</p>
          <ul className="export-files">
            {exportResult.files.map((file) => (
              <li key={file}>{file}</li>
            ))}
          </ul>
          {exportResult.skipped.length > 0 && (
            <ul className="export-skipped">
              {exportResult.skipped.map((item) => (
                <li key={item.capture_id}>{item.capture_id}: {item.reason}</li>
              ))}
            </ul>
          )}
          <div className="export-import-row">
            <button className="secondary-action" onClick={onImport} disabled={importing}>
              {importing ? "登録しています…" : "Apple フォトへ登録"}
            </button>
            <small>アルバム名: {albumName}</small>
          </div>
          {importResult && (
            <p className={`import-result${importResult.status === "ok" ? " ok" : " error"}`}>
              {importResult.status === "ok"
                ? `${importResult.imported}件を「${importResult.album}」に登録しました。`
                : importResult.error}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
