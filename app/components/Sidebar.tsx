import { assetUrl } from "../lib/raw-api";
import { formatBytes } from "../lib/format";
import type { Analysis } from "../lib/types";

type SidebarProps = {
  analysis: Analysis | null;
  coverThumb: string | null;
};

export function Sidebar({ analysis, coverThumb }: SidebarProps) {
  const source = analysis?.source ?? null;
  const missing = source ? source.jpg_only + source.arw_only : 0;

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

      <div className="sidebar-section">
        <p className="eyebrow">現在のバッチ</p>
        <div className="batch-card">
          <div className="batch-cover">
            {coverThumb ? (
              <img src={assetUrl(coverThumb)} alt="現在の撮影バッチ" loading="lazy" decoding="async" />
            ) : (
              <span className="batch-cover-empty" aria-hidden="true" />
            )}
          </div>
          <div>
            <strong>{source ? source.root : "未読み込み"}</strong>
            <small>{source ? `${source.pairs}ペア・${missing === 0 ? "欠落なし" : `欠落${missing}件`}` : "解析前"}</small>
          </div>
          <span className="status-dot" title={analysis ? "分析完了" : "未分析"} />
        </div>
      </div>

      <div className="sidebar-spacer" />
      <div className="storage">
        <div><span>ローカル処理</span><strong>{source ? formatBytes(source.total_bytes) : "—"}</strong></div>
        <div className="storage-track"><span /></div>
        <small>画像は外部へ送信されません</small>
      </div>
      <button className="settings-button"><span>⚙</span>設定</button>
    </aside>
  );
}
