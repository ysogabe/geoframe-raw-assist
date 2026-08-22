import type { PhotoExportVariant } from "../lib/app-state";
import { isValidBatchName } from "../lib/app-state";
import { rawPillClass } from "../lib/format";
import { assetUrl } from "../lib/raw-api";
import type {
  Analysis,
  CaptureId,
  DevelopedResult,
  DriveUploadState,
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
  /** GET /api/drive-upload の最新状況。null は未取得（エンジン未起動・未実装）。 */
  driveUpload: DriveUploadState | null;
  onDriveUpload: () => void;
  onBack: () => void;
};

/** 実行中ボタンに出すステージ名。未知の値は「処理中…」へ落とす。 */
function driveUploadStageLabel(stage: string): string {
  switch (stage) {
    case "authenticating":
      return "認証を確認しています…";
    case "waiting_for_consent":
      return "ブラウザでサインインしてください…";
    case "resolving_folder":
      return "フォルダを確認しています…";
    case "uploading":
      return "アップロード中…";
    default:
      return "処理中…";
  }
}

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
  driveUpload,
  onDriveUpload,
  onBack,
}: ExportPanelProps) {
  const nameValid = isValidBatchName(batchName);
  const canExport = rows.length > 0 && nameValid && !exporting;

  // サーバの Drive ジョブ状態は **プロセス全体で単一スロット**（local_raw/drive_upload.py
  // のモジュールグローバル）なので、GET /api/drive-upload は今この画面で扱っている
  // バッチとは無関係なジョブを返しうる。照合せずに描くと、別バッチの進捗・完了件数・
  // Drive フォルダのリンクをこのバッチの結果として出してしまう。
  //
  // running / done / error は batch_name を持つので、一致しないものは「このバッチに
  // ついては未取得」（= null）と同じ扱いにする。idle は特定のバッチに紐づかない
  // グローバルな設定状態（configured）なので、バッチ名に関わらずそのまま使う。
  const driveUploadForBatch =
    driveUpload === null || driveUpload.status === "idle" || driveUpload.batch_name === batchName
      ? driveUpload
      : null;

  // Drive アップロードの活性制御。状況が未取得（null）のうちは押せる状態にしておく
  // （エンジンが未対応なら POST 側が失敗してトーストで理由が出る）。
  const driveUploadRunning = driveUploadForBatch?.status === "running";
  const driveUploadConfigured =
    driveUploadForBatch === null || driveUploadForBatch.status !== "idle" || driveUploadForBatch.configured;
  const driveUploadDisabled = driveUploadRunning || !driveUploadConfigured;
  const driveUploadDisabledReason = driveUploadConfigured
    ? undefined
    : "Google Drive 連携が未設定です。local_raw/drive_client_secret.json を配置してください。";
  const driveUploadButtonLabel =
    driveUploadForBatch?.status === "running"
      ? `${driveUploadStageLabel(driveUploadForBatch.progress.stage)}（${driveUploadForBatch.progress.done}/${driveUploadForBatch.progress.total}）`
      : "Google Drive にアップロード";

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
            <button
              className="secondary-action"
              onClick={onDriveUpload}
              disabled={driveUploadDisabled}
              title={driveUploadDisabledReason}
            >
              {driveUploadButtonLabel}
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
          {driveUploadForBatch?.status === "done" && (
            <p className="drive-upload-result ok">
              {driveUploadForBatch.uploaded}件を Google Drive にアップロードしました
              {driveUploadForBatch.skipped.length > 0 ? `（${driveUploadForBatch.skipped.length}件は既存のためスキップ）` : ""}。
              {" "}
              <a href={driveUploadForBatch.folder_url} target="_blank" rel="noreferrer">
                フォルダを開く
              </a>
            </p>
          )}
          {driveUploadForBatch?.status === "error" && (
            <p className="drive-upload-result error">{driveUploadForBatch.error}</p>
          )}
        </div>
      )}
    </section>
  );
}
