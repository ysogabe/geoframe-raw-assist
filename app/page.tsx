"use client";

import { useEffect, useMemo, useRef, useState } from "react";

type Shot = {
  id: string;
  image: string;
  score: number;
  sharpness: number;
  highlight: number;
  label?: string;
};

type Group = {
  id: number;
  title: string;
  kind: string;
  time: string;
  recommendation: string;
  reason: string;
  raw: "none" | "maybe" | "recommended" | "strong";
  rawLabel: string;
  shots: Shot[];
};

type RawPreset = "neutral_recovery" | "landscape_clear" | "nightscape";

type DevelopedResult = {
  capture_id: string;
  preset: RawPreset;
  preset_label: string;
  output_url: string;
  width: number;
  height: number;
  elapsed_seconds: number;
  cached: boolean;
};

const LOCAL_RAW_API = "http://127.0.0.1:8788";

const groups: Group[] = [
  {
    id: 1,
    title: "花のクローズアップ",
    kind: "類似カット",
    time: "10:42",
    recommendation: "DSC02774",
    reason: "花芯の輪郭が最も明瞭で、背景のボケも自然です。",
    raw: "maybe",
    rawLabel: "調整で改善",
    shots: [
      { id: "DSC02773", image: "/photos/DSC02773.webp", score: 72, sharpness: 68, highlight: 94 },
      { id: "DSC02774", image: "/photos/DSC02774.webp", score: 91, sharpness: 94, highlight: 91, label: "推薦" },
      { id: "DSC02775", image: "/photos/DSC02775.webp", score: 79, sharpness: 82, highlight: 88 },
    ],
  },
  {
    id: 2,
    title: "地下鉄のモザイク",
    kind: "連写 3枚",
    time: "10:47",
    recommendation: "DSC02776",
    reason: "円形装飾が中央に収まり、手ブレが最も少ない一枚です。",
    raw: "recommended",
    rawLabel: "RAW推奨",
    shots: [
      { id: "DSC02776", image: "/photos/DSC02776.webp", score: 89, sharpness: 91, highlight: 87, label: "推薦" },
      { id: "DSC02777", image: "/photos/DSC02777.webp", score: 81, sharpness: 83, highlight: 84 },
      { id: "DSC02778", image: "/photos/DSC02778.webp", score: 84, sharpness: 86, highlight: 86 },
    ],
  },
  {
    id: 3,
    title: "旧市街の屋上",
    kind: "類似カット",
    time: "13:15",
    recommendation: "DSC02861",
    reason: "水平が安定し、建物と遠景の階調が最も残っています。",
    raw: "none",
    rawLabel: "JPEGで採用可",
    shots: [
      { id: "DSC02860", image: "/photos/DSC02860.webp", score: 76, sharpness: 80, highlight: 78 },
      { id: "DSC02861", image: "/photos/DSC02861.webp", score: 92, sharpness: 90, highlight: 95, label: "推薦" },
      { id: "DSC02862", image: "/photos/DSC02862.webp", score: 86, sharpness: 87, highlight: 89 },
    ],
  },
  {
    id: 4,
    title: "木彫と回廊",
    kind: "構図違い",
    time: "13:28",
    recommendation: "DSC02869",
    reason: "回廊の奥行きと装飾の情報量が両立しています。",
    raw: "maybe",
    rawLabel: "調整で改善",
    shots: [
      { id: "DSC02867", image: "/photos/DSC02867.webp", score: 75, sharpness: 85, highlight: 67 },
      { id: "DSC02868", image: "/photos/DSC02868.webp", score: 78, sharpness: 82, highlight: 73 },
      { id: "DSC02869", image: "/photos/DSC02869.webp", score: 88, sharpness: 89, highlight: 86, label: "推薦" },
    ],
  },
  {
    id: 5,
    title: "ライトアップされた門",
    kind: "夜景",
    time: "20:06",
    recommendation: "DSC02943",
    reason: "建物の輪郭が最もシャープで、照明の色も残っています。",
    raw: "strong",
    rawLabel: "RAWを強く推奨",
    shots: [
      { id: "DSC02943", image: "/photos/DSC02943.webp", score: 94, sharpness: 92, highlight: 88, label: "推薦" },
      { id: "DSC02946", image: "/photos/DSC02946.webp", score: 82, sharpness: 84, highlight: 79 },
      { id: "DSC02947", image: "/photos/DSC02947.webp", score: 85, sharpness: 87, highlight: 81 },
    ],
  },
  {
    id: 6,
    title: "夕暮れの広場",
    kind: "逆光 3枚",
    time: "20:21",
    recommendation: "DSC02954",
    reason: "空のハイライトを保ちながら、建物の暗部も残っています。",
    raw: "recommended",
    rawLabel: "RAW推奨",
    shots: [
      { id: "DSC02948", image: "/photos/DSC02948.webp", score: 74, sharpness: 80, highlight: 63 },
      { id: "DSC02953", image: "/photos/DSC02953.webp", score: 86, sharpness: 84, highlight: 91 },
      { id: "DSC02954", image: "/photos/DSC02954.webp", score: 90, sharpness: 88, highlight: 95, label: "推薦" },
    ],
  },
];

const rawClass = (level: Group["raw"]) => `raw-pill raw-${level}`;

export default function Home() {
  const [selectedGroupId, setSelectedGroupId] = useState(1);
  const [selectedShots, setSelectedShots] = useState<Record<number, string>>(
    Object.fromEntries(groups.map((group) => [group.id, group.recommendation])),
  );
  const [accepted, setAccepted] = useState<number[]>([]);
  const [queued, setQueued] = useState<number[]>([]);
  const [activeView, setActiveView] = useState<"suggestions" | "queue" | "accepted">("suggestions");
  const [notice, setNotice] = useState("");
  const [engineStatus, setEngineStatus] = useState<"checking" | "ready" | "offline">("checking");
  const [engineVersion, setEngineVersion] = useState("");
  const [rawPreset, setRawPreset] = useState<RawPreset>("neutral_recovery");
  const [processingCapture, setProcessingCapture] = useState("");
  const [developed, setDeveloped] = useState<Record<string, DevelopedResult>>({});
  const [screen, setScreen] = useState<"suggestions" | "operation">("suggestions");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const comparisonRef = useRef<HTMLDivElement>(null);

  const selectedGroup = groups.find((group) => group.id === selectedGroupId) ?? groups[0];
  const selectedShot = selectedGroup.shots.find((shot) => shot.id === selectedShots[selectedGroup.id]) ?? selectedGroup.shots[0];
  const visibleGroups = useMemo(() => {
    if (activeView === "accepted") return groups.filter((group) => accepted.includes(group.id));
    if (activeView === "queue") return groups.filter((group) => queued.includes(group.id));
    return groups;
  }, [accepted, activeView, queued]);

  useEffect(() => {
    fetch(`${LOCAL_RAW_API}/api/health`)
      .then((response) => {
        if (!response.ok) throw new Error("offline");
        return response.json();
      })
      .then((payload) => {
        setEngineVersion(`rawpy ${payload.rawpy_version}`);
        setEngineStatus("ready");
      })
      .catch(() => setEngineStatus("offline"));
  }, []);

  useEffect(() => {
    setRawPreset(selectedGroup.raw === "strong" ? "nightscape" : selectedGroup.raw === "none" ? "landscape_clear" : "neutral_recovery");
  }, [selectedGroup.id, selectedGroup.raw]);

  function openOperationScreen(groupId: number) {
    setSelectedGroupId(groupId);
    setScreen("operation");
    window.requestAnimationFrame(() => {
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    });
  }

  function returnToSuggestions() {
    setScreen("suggestions");
    window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "smooth" }));
  }

  function chooseShot(groupId: number, shotId: string) {
    setSelectedShots((current) => ({ ...current, [groupId]: shotId }));
  }

  function acceptSelection() {
    setAccepted((current) => (current.includes(selectedGroup.id) ? current : [...current, selectedGroup.id]));
    setNotice(`${selectedShots[selectedGroup.id]} を採用しました`);
  }

  async function queueRaw() {
    if (engineStatus !== "ready") {
      setNotice("ローカルRAWエンジンが停止中です。npm run dev:raw で起動してください");
      return;
    }

    const captureId = selectedShots[selectedGroup.id];
    setProcessingCapture(captureId);
    setNotice(`${captureId}.ARW を現像しています…`);
    try {
      const response = await fetch(`${LOCAL_RAW_API}/api/raw-develop`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ capture_id: captureId, preset: rawPreset, max_edge: 6000 }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "RAW現像に失敗しました");
      setDeveloped((current) => ({ ...current, [captureId]: result }));
      setQueued((current) => (current.includes(selectedGroup.id) ? current : [...current, selectedGroup.id]));
      setNotice(`${captureId} のRAW現像が完了しました（${result.elapsed_seconds.toFixed(1)}秒）`);
      window.setTimeout(() => comparisonRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 120);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "RAW現像に失敗しました");
    } finally {
      setProcessingCapture("");
    }
  }

  function handleFiles(files: FileList | null) {
    if (!files?.length) return;
    const jpgs = Array.from(files).filter((file) => /\.jpe?g$/i.test(file.name)).length;
    const raws = Array.from(files).filter((file) => /\.arw$/i.test(file.name)).length;
    setNotice(`${files.length}ファイルを確認しました（JPG ${jpgs} / ARW ${raws}）`);
  }

  return (
    <div className="app-shell">
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

      <main className="workspace">
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
                <input ref={fileInputRef} className="hidden-input" type="file" multiple accept=".jpg,.jpeg,.arw" onChange={(event) => handleFiles(event.target.files)} />
              </div>
            </>
          ) : (
            <>
              <div>
                <button className="page-back" onClick={returnToSuggestions}><span>←</span> おすすめ一覧</button>
                <div className="breadcrumbs"><span>レビュー</span><b>/</b>操作パネル</div>
                <h1>{selectedGroup.title}</h1>
                <p>{selectedShot.id}.JPGを比較し、採用とRAW現像を決めます。</p>
              </div>
              <div className="operation-status"><span className={`engine-badge ${engineStatus}`}><i />{engineStatus === "ready" ? `${engineVersion} 接続済み` : "RAWエンジン停止中"}</span><strong>{selectedGroup.kind}</strong></div>
            </>
          )}
        </header>

        {screen === "suggestions" && (
          <section className="summary-strip" aria-label="分析サマリー">
            <div className="summary-lead"><span className="pulse">✓</span><div><strong>281組のペアを解析しました</strong><small>JPGとARWの欠落はありません</small></div></div>
            <div className="summary-metric"><strong>54</strong><span>撮影グループ</span></div>
            <div className="summary-metric"><strong>18</strong><span>RAW現像候補</span></div>
            <div className="summary-metric"><strong>6</strong><span>確認が必要</span></div>
            <div className="progress-ring" aria-label="分析完了 100%"><span>100<small>%</small></span></div>
          </section>
        )}

        {notice && <div className="toast" role="status"><span>✓</span>{notice}<button onClick={() => setNotice("")} aria-label="閉じる">×</button></div>}

        {screen === "suggestions" ? (
          <section className="review-list screen-enter">
            <div className="section-heading">
              <div><p className="eyebrow">SMART REVIEW</p><h2>おすすめを確認</h2></div>
              <div className="view-tabs" role="tablist" aria-label="表示切替">
                <button className={activeView === "suggestions" ? "active" : ""} onClick={() => setActiveView("suggestions")}>すべて <span>{groups.length}</span></button>
                <button className={activeView === "queue" ? "active" : ""} onClick={() => setActiveView("queue")}>現像済み <span>{queued.length}</span></button>
                <button className={activeView === "accepted" ? "active" : ""} onClick={() => setActiveView("accepted")}>採用済み <span>{accepted.length}</span></button>
              </div>
            </div>

            <div className="group-stack">
              {visibleGroups.length ? visibleGroups.map((group) => {
                const hero = group.shots.find((shot) => shot.id === selectedShots[group.id]) ?? group.shots[0];
                return (
                  <article key={group.id} className="group-card" onClick={() => openOperationScreen(group.id)}>
                    <div className="group-hero">
                      <img src={hero.image} alt={`${group.title} ${hero.id}`} />
                      <span className="photo-count">▧ {group.shots.length}枚</span>
                      {accepted.includes(group.id) && <span className="accepted-badge">✓ 採用済み</span>}
                    </div>
                    <div className="group-copy">
                      <div className="group-meta"><span>{group.kind}</span><small>{group.time}</small></div>
                      <h3>{group.title}</h3>
                      <div className="recommendation"><span className="spark">✦</span><p><strong>{selectedShots[group.id]}</strong><small>{group.reason}</small></p></div>
                      <div className="group-footer"><span className={rawClass(group.raw)}>{group.rawLabel}</span><button onClick={(event) => { event.stopPropagation(); openOperationScreen(group.id); }}>操作パネルを開く <b>→</b></button></div>
                    </div>
                  </article>
                );
              }) : (
                <div className="empty-state"><span>○</span><h3>まだ写真がありません</h3><p>別の表示に切り替えるか、提案から写真を採用してください。</p></div>
              )}
            </div>
          </section>
        ) : (
          <section className="operation-screen screen-enter" aria-label="写真の操作パネル">
            <div className="operation-progress"><span className="done">✓</span><div><strong>候補を選択</strong><small>{selectedGroup.shots.length}枚から比較</small></div><i /><span className="current">2</span><div><strong>仕上げを決定</strong><small>採用またはRAW現像</small></div></div>

            <div className="operation-layout">
              <div className="operation-media-card">
                <div className="media-heading"><div><p className="eyebrow">SELECTED PHOTO</p><h2>{selectedShot.id}</h2></div><span className={rawClass(selectedGroup.raw)}>{selectedGroup.rawLabel}</span></div>
                <div className="selected-preview operation-preview">
                  <img src={selectedShot.image} alt={`選択中 ${selectedShot.id}`} />
                  <div className="preview-grade"><small>総合</small><strong>{selectedShot.score}</strong></div>
                  <div className="filename">{selectedShot.id}.JPG</div>
                </div>
                <div className="shot-picker operation-shot-picker" role="radiogroup" aria-label="代表写真を選択">
                  {selectedGroup.shots.map((shot) => (
                    <button key={shot.id} className={selectedShots[selectedGroup.id] === shot.id ? "active" : ""} onClick={() => chooseShot(selectedGroup.id, shot.id)} role="radio" aria-checked={selectedShots[selectedGroup.id] === shot.id}>
                      <img src={shot.image} alt={shot.id} />
                      {shot.label && <span>{shot.label}</span>}
                      <small>{shot.id}</small>
                    </button>
                  ))}
                </div>
              </div>

              <aside className="operation-controls-card">
                <div className="controls-heading"><p className="eyebrow">DECISION</p><h2>この写真をどう仕上げますか？</h2></div>
                <div className="analysis-card">
                  <div className="analysis-title"><span className="spark">✦</span><strong>この一枚をおすすめ</strong><small>信頼度 {selectedShot.score}%</small></div>
                  <p>{selectedGroup.reason}</p>
                  <div className="score-row"><span>シャープネス</span><div><i style={{ width: `${selectedShot.sharpness}%` }} /></div><strong>{selectedShot.sharpness}</strong></div>
                  <div className="score-row"><span>階調の保持</span><div><i style={{ width: `${selectedShot.highlight}%` }} /></div><strong>{selectedShot.highlight}</strong></div>
                  <div className="score-row"><span>構図</span><div><i style={{ width: `${selectedShot.score - 3}%` }} /></div><strong>{selectedShot.score - 3}</strong></div>
                </div>
                <div className={`raw-callout ${selectedGroup.raw === "strong" ? "urgent" : ""}`}><span className="raw-icon">R</span><div><strong>{selectedGroup.rawLabel}</strong><p>{selectedGroup.raw === "none" ? "JPEGの階調が十分に残っています。" : selectedGroup.raw === "strong" ? "夜景・高ISO・深い暗部を検出。ノイズ低減と色調整の効果が期待できます。" : "暗部とハイライトに調整余地があります。"}</p></div></div>
                <div className="raw-controls">
                  <div className="engine-row"><label htmlFor="raw-preset">現像プリセット</label><span className={`engine-badge ${engineStatus}`}><i />{engineStatus === "ready" ? `${engineVersion} 接続済み` : engineStatus === "checking" ? "確認中" : "ローカルエンジン停止中"}</span></div>
                  <select id="raw-preset" value={rawPreset} onChange={(event) => setRawPreset(event.target.value as RawPreset)}><option value="neutral_recovery">階調回復 — 自然な明暗差</option><option value="landscape_clear">風景クリア — 彩度と輪郭</option><option value="nightscape">夜景ノイズ低減 — 高ISO向け</option></select>
                </div>
                <div className="inspector-actions operation-actions"><button className="primary-action" onClick={acceptSelection}>{accepted.includes(selectedGroup.id) ? "✓ 採用済み" : "この写真を採用"}</button><button className="secondary-action" onClick={queueRaw} disabled={selectedGroup.raw === "none" || processingCapture === selectedShot.id}>{processingCapture === selectedShot.id ? "現像中…" : developed[selectedShot.id] ? "✓ 現像済み" : "RAW現像を実行"}</button></div>
                <p className="safety-note">元のJPG / ARWは変更されません</p>
              </aside>
            </div>

            {developed[selectedShot.id] && (
              <div ref={comparisonRef} className="developed-comparison operation-comparison">
                <div className="developed-head"><div><span className="comparison-check">✓</span><strong>現像前後を比較</strong></div><span>{developed[selectedShot.id].preset_label}</span></div>
                <div className="before-after-grid">
                  <figure><div className="comparison-image"><img src={`${LOCAL_RAW_API}/source-jpg/${selectedShot.id}.JPG`} alt={`${selectedShot.id} 現像前のJPG`} /></div><figcaption><strong>現像前</strong><span>カメラ内 JPG</span></figcaption></figure>
                  <figure><a className="comparison-image" href={developed[selectedShot.id].output_url} target="_blank" rel="noreferrer"><img src={developed[selectedShot.id].output_url} alt={`${selectedShot.id} RAW現像後`} /><span className="developed-label">原寸で見る ↗</span></a><figcaption><strong>現像後</strong><span>ARW / {developed[selectedShot.id].preset_label}</span></figcaption></figure>
                </div>
                <div className="developed-meta"><span>{developed[selectedShot.id].width} × {developed[selectedShot.id].height}px</span><span>{developed[selectedShot.id].cached ? "保存済み結果" : `${developed[selectedShot.id].elapsed_seconds.toFixed(1)}秒`}</span></div>
              </div>
            )}
          </section>
        )}
      </main>
    </div>
  );
}
