import type { EngineStatus, Screen } from "../lib/app-state";

type TopbarProps = {
  screen: Screen;
  groupTitle: string;
  groupKind: string;
  captureId: string;
  engineStatus: EngineStatus;
  engineVersion: string;
  canExport: boolean;
  acceptedCount: number;
  analysisReady: boolean;
  reanalyzing: boolean;
  onReturn: () => void;
  onOpenExport: () => void;
  onReanalyze: () => void;
};

function engineLabel(engineStatus: EngineStatus, engineVersion: string) {
  if (engineStatus === "ready") return `${engineVersion} 接続済み`;
  if (engineStatus === "checking") return "RAWエンジンを確認中";
  return "RAWエンジン停止中";
}

export function Topbar({
  screen,
  groupTitle,
  groupKind,
  captureId,
  engineStatus,
  engineVersion,
  canExport,
  acceptedCount,
  analysisReady,
  reanalyzing,
  onReturn,
  onOpenExport,
  onReanalyze,
}: TopbarProps) {
  if (screen === "suggestions") {
    return (
      <header className="topbar">
        <div>
          <div className="breadcrumbs"><span>レビュー</span><b>/</b>おすすめ一覧</div>
          <h1>おすすめ写真の確認</h1>
          <p>解析結果から似たカットを比較し、採用する写真とRAW現像の対象を選びます。</p>
        </div>
        <div className="top-actions">
          <span className={`engine-badge ${engineStatus}`}><i />{engineLabel(engineStatus, engineVersion)}</span>
          <button className="icon-button" onClick={onReanalyze} disabled={!analysisReady || reanalyzing} title="再解析">
            ↻
          </button>
          <button className="import-button" onClick={onOpenExport} disabled={!canExport}>
            <span>▤</span>書き出しへ{acceptedCount > 0 ? `（${acceptedCount}）` : ""}
          </button>
        </div>
      </header>
    );
  }

  if (screen === "export") {
    return (
      <header className="topbar operation-topbar">
        <div>
          <button className="page-back" onClick={onReturn}><span>←</span> おすすめ一覧</button>
          <div className="breadcrumbs"><span>レビュー</span><b>/</b>書き出し</div>
          <h1>採用した写真を書き出す</h1>
          <p>カメラJPGか現像済みJPEGを選び、バッチ名を付けてアプリのデータフォルダ（export/）へコピーします。</p>
        </div>
        <div className="operation-status">
          <span className={`engine-badge ${engineStatus}`}><i />{engineLabel(engineStatus, engineVersion)}</span>
          <strong>採用 {acceptedCount}件</strong>
        </div>
      </header>
    );
  }

  return (
    <header className="topbar operation-topbar">
      <div>
        <button className="page-back" onClick={onReturn}><span>←</span> おすすめ一覧</button>
        <div className="breadcrumbs"><span>レビュー</span><b>/</b>操作パネル</div>
        <h1>{groupTitle}</h1>
        <p>{captureId}.JPGを比較し、採用とRAW現像を決めます。</p>
      </div>
      <div className="operation-status">
        <span className={`engine-badge ${engineStatus}`}><i />{engineLabel(engineStatus, engineVersion)}</span>
        <strong>{groupKind}</strong>
      </div>
    </header>
  );
}
