import { useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { assetUrl } from "../lib/raw-api";
import { sourceFolderIssue } from "../lib/format";
import { isTauri } from "../lib/setup";
import type { AnalysisSource, SourceFolderInfo } from "../lib/types";

type SourceFolderPanelProps = {
  /** GET /api/source-folder の結果。未取得・未対応なら null。 */
  info: SourceFolderInfo | null;
  /** エンドポイントが 404 / 接続不能。フォルダ変更UIは出さず、従来の表示だけを続ける。 */
  unavailable: boolean;
  /** 直近の解析結果に含まれるフォルダ情報。info が無いときだけのフォールバック。 */
  fallbackSource: AnalysisSource | null;
  coverThumb: string | null;
  /** POST 実行中。 */
  busy: boolean;
  /**
   * 解析実行中。サーバ側も analysis_running で弾くが、初回解析は数十秒かかるので
   * 「押せるのに必ず失敗する」状態を作らないよう UI 側でも操作を止める。
   */
  locked: boolean;
  onApply: (path: string) => void;
  onError: (message: string) => void;
};

export function SourceFolderPanel({
  info,
  unavailable,
  fallbackSource,
  coverThumb,
  busy,
  locked,
  onApply,
  onError,
}: SourceFolderPanelProps) {
  const [draftPath, setDraftPath] = useState("");

  const pathLabel = info ? info.display_path : (fallbackSource?.root ?? "未読み込み");
  const pairs = info ? info.pairs : (fallbackSource?.pairs ?? null);
  const missing = info
    ? info.jpg_only + info.arw_only
    : fallbackSource
      ? fallbackSource.jpg_only + fallbackSource.arw_only
      : 0;
  const issue = info ? sourceFolderIssue(info) : null;
  const disabled = busy || locked;

  const summary =
    pairs === null
      ? "解析前"
      : `${pairs}ペア・${missing === 0 ? "欠落なし" : `欠落${missing}件`}`;

  async function pickFolder() {
    try {
      // 選択肢をインラインで書くこと（変数に切り出すと multiple が boolean に広がり、
      // 返り値が string | string[] | null になる）。
      const selected = await open({ directory: true, multiple: false });
      // キャンセルは null。何もしない。
      if (typeof selected !== "string" || !selected) return;
      onApply(selected);
    } catch (error) {
      onError(
        error instanceof Error && error.message
          ? error.message
          : "フォルダ選択ダイアログを開けませんでした。",
      );
    }
  }

  function applyDraft() {
    const trimmed = draftPath.trim();
    if (!trimmed) return;
    onApply(trimmed);
  }

  return (
    <div className="sidebar-section">
      <p className="eyebrow">現像対象フォルダ</p>

      <div className="batch-card">
        <div className="batch-cover">
          {coverThumb ? (
            <img src={assetUrl(coverThumb)} alt="現在の撮影バッチ" loading="lazy" decoding="async" />
          ) : (
            <span className="batch-cover-empty" aria-hidden="true" />
          )}
        </div>
        <div>
          <strong title={info ? info.path : pathLabel}>{pathLabel}</strong>
          <small>{summary}</small>
        </div>
        <span
          className={`status-dot${issue ? " warn" : ""}`}
          title={issue ?? (info ? "読み取り可能" : "未取得")}
        />
      </div>

      {info?.is_default && !issue && <p className="source-folder-note">既定のサンプルフォルダです</p>}
      {issue && (
        <p className="source-folder-note warn" role="status">
          {issue}
        </p>
      )}

      {!unavailable &&
        (isTauri() ? (
          <button className="source-folder-button" onClick={() => void pickFolder()} disabled={disabled}>
            <span aria-hidden="true">◱</span>
            {busy ? "変更しています…" : "フォルダを選ぶ"}
          </button>
        ) : (
          // ブラウザ実行（npm run dev / dev:raw）ではネイティブダイアログが使えない。
          <div className="source-folder-form">
            <label className="source-folder-label" htmlFor="source-folder-path">
              フォルダの絶対パス
            </label>
            <input
              id="source-folder-path"
              className="source-folder-input"
              type="text"
              value={draftPath}
              placeholder="/Users/…/100MSDCF"
              spellCheck={false}
              autoComplete="off"
              disabled={disabled}
              onChange={(event) => setDraftPath(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") applyDraft();
              }}
            />
            <button
              className="source-folder-button"
              onClick={applyDraft}
              disabled={disabled || draftPath.trim().length === 0}
            >
              {busy ? "変更しています…" : "適用"}
            </button>
          </div>
        ))}

      {!unavailable && locked && <p className="source-folder-note">解析中は変更できません</p>}
    </div>
  );
}
