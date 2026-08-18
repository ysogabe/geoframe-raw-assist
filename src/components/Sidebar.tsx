import { formatBytes } from "../lib/format";
import { SourceFolderPanel } from "./SourceFolderPanel";
import type { Analysis, SourceFolderInfo } from "../lib/types";

type SidebarProps = {
  analysis: Analysis | null;
  coverThumb: string | null;
  sourceFolder: SourceFolderInfo | null;
  sourceFolderUnavailable: boolean;
  sourceFolderBusy: boolean;
  sourceFolderLocked: boolean;
  onApplySourceFolder: (path: string) => void;
  onSourceFolderError: (message: string) => void;
};

export function Sidebar({
  analysis,
  coverThumb,
  sourceFolder,
  sourceFolderUnavailable,
  sourceFolderBusy,
  sourceFolderLocked,
  onApplySourceFolder,
  onSourceFolderError,
}: SidebarProps) {
  const source = analysis?.source ?? null;
  // 総容量は「直近の解析時点」の値を優先する（sourceFolder 側は保存時スキャンの
  // 値で、以後 GET し直さないため古くなりうる）。解析前だけフォルダ情報で埋める。
  const totalBytes = source ? source.total_bytes : (sourceFolder?.total_bytes ?? null);

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /></span>
        <span><strong>GeoFrame</strong><small>RAW ASSIST</small></span>
      </div>

      <nav className="primary-nav" aria-label="メインナビゲーション">
        <button className="nav-item active"><span>◫</span>レビュー</button>
        <button className="nav-item"><span>⌁</span>ライブラリ</button>
        <button className="nav-item"><span>↻</span>処理履歴</button>
      </nav>

      <SourceFolderPanel
        info={sourceFolder}
        unavailable={sourceFolderUnavailable}
        fallbackSource={source}
        coverThumb={coverThumb}
        busy={sourceFolderBusy}
        locked={sourceFolderLocked}
        onApply={onApplySourceFolder}
        onError={onSourceFolderError}
      />

      <div className="sidebar-spacer" />
      <div className="storage">
        <div><span>ローカル処理</span><strong>{totalBytes === null ? "—" : formatBytes(totalBytes)}</strong></div>
        <div className="storage-track"><span /></div>
        <small>画像は外部へ送信されません</small>
      </div>
      <button className="settings-button"><span>⚙</span>設定</button>
    </aside>
  );
}
