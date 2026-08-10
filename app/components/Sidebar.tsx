export function Sidebar() {
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
          <div className="batch-cover"><img src="/photos/DSC02943.webp" alt="現在の撮影バッチ" /></div>
          <div><strong>旅写真 #08</strong><small>281ペア・分析完了</small></div>
          <span className="status-dot" title="分析完了" />
        </div>
      </div>

      <div className="sidebar-spacer" />
      <div className="storage">
        <div><span>ローカル処理</span><strong>10.0 GB</strong></div>
        <div className="storage-track"><span /></div>
        <small>画像は外部へ送信されません</small>
      </div>
      <button className="settings-button"><span>⚙</span>設定</button>
    </aside>
  );
}
