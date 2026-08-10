import { useRef } from "react";

type TopbarProps = {
  screen: "suggestions" | "operation";
  groupTitle: string;
  groupKind: string;
  shotId: string;
  engineStatus: "checking" | "ready" | "offline";
  engineVersion: string;
  onReturn: () => void;
  onFilesSelected: (files: FileList | null) => void;
};

export function Topbar({ screen, groupTitle, groupKind, shotId, engineStatus, engineVersion, onReturn, onFilesSelected }: TopbarProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    <header className={`topbar ${screen === "operation" ? "operation-topbar" : ""}`}>
      {screen === "suggestions" ? (
        <>
          <div>
            <div className="breadcrumbs"><span>レビュー</span><b>/</b>旅写真 #08</div>
            <h1>選ぶ時間を、撮る時間へ。</h1>
            <p>似たカットをまとめて比較し、仕上げるべきRAWだけを提案します。</p>
          </div>
          <div className="top-actions">
            <button className="icon-button" aria-label="通知">●<span /></button>
            <button className="import-button" onClick={() => fileInputRef.current?.click()}><span>＋</span>フォルダを追加</button>
            <input ref={fileInputRef} className="hidden-input" type="file" multiple accept=".jpg,.jpeg,.arw" onChange={(event) => onFilesSelected(event.target.files)} />
          </div>
        </>
      ) : (
        <>
          <div>
            <button className="page-back" onClick={onReturn}><span>←</span> おすすめ一覧</button>
            <div className="breadcrumbs"><span>レビュー</span><b>/</b>操作パネル</div>
            <h1>{groupTitle}</h1>
            <p>{shotId}.JPGを比較し、採用とRAW現像を決めます。</p>
          </div>
          <div className="operation-status"><span className={`engine-badge ${engineStatus}`}><i />{engineStatus === "ready" ? `${engineVersion} 接続済み` : "RAWエンジン停止中"}</span><strong>{groupKind}</strong></div>
        </>
      )}
    </header>
  );
}
