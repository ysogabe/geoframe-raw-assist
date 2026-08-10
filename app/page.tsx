"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { DevelopedResult, RawPreset } from "./lib/types";
import { fetchHealth, developRaw } from "./lib/raw-api";
import { groups } from "./data/mock-groups";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { SummaryStrip } from "./components/SummaryStrip";
import { GroupList } from "./components/GroupList";
import { OperationPanel } from "./components/OperationPanel";

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
  const comparisonRef = useRef<HTMLDivElement>(null);

  const selectedGroup = groups.find((group) => group.id === selectedGroupId) ?? groups[0];
  const selectedShot = selectedGroup.shots.find((shot) => shot.id === selectedShots[selectedGroup.id]) ?? selectedGroup.shots[0];
  const visibleGroups = useMemo(() => {
    if (activeView === "accepted") return groups.filter((group) => accepted.includes(group.id));
    if (activeView === "queue") return groups.filter((group) => queued.includes(group.id));
    return groups;
  }, [accepted, activeView, queued]);

  useEffect(() => {
    fetchHealth()
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
      const result = await developRaw(captureId, rawPreset, 6000);
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
      <Sidebar />

      <main className="workspace">
        <Topbar
          screen={screen}
          groupTitle={selectedGroup.title}
          groupKind={selectedGroup.kind}
          shotId={selectedShot.id}
          engineStatus={engineStatus}
          engineVersion={engineVersion}
          onReturn={returnToSuggestions}
          onFilesSelected={handleFiles}
        />

        {screen === "suggestions" && <SummaryStrip />}

        {notice && <div className="toast" role="status"><span>✓</span>{notice}<button onClick={() => setNotice("")} aria-label="閉じる">×</button></div>}

        {screen === "suggestions" ? (
          <GroupList
            groups={groups}
            visibleGroups={visibleGroups}
            selectedShots={selectedShots}
            accepted={accepted}
            queued={queued}
            activeView={activeView}
            onChangeView={setActiveView}
            onOpenGroup={openOperationScreen}
          />
        ) : (
          <OperationPanel
            selectedGroup={selectedGroup}
            selectedShot={selectedShot}
            selectedShots={selectedShots}
            onChooseShot={chooseShot}
            engineStatus={engineStatus}
            engineVersion={engineVersion}
            rawPreset={rawPreset}
            onChangePreset={setRawPreset}
            accepted={accepted}
            onAccept={acceptSelection}
            onQueueRaw={queueRaw}
            processingCapture={processingCapture}
            developed={developed}
            comparisonRef={comparisonRef}
          />
        )}
      </main>
    </div>
  );
}
