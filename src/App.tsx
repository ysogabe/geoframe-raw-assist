import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  activePreset,
  changedAdjustmentKeys,
  developedGroupIds,
  effectiveAdjustments,
  initialState,
  isValidBatchName,
  photoOf,
  reducer,
  selectedCaptureOf,
  selectedGroup,
  visibleGroups,
  type DevelopMode,
} from "./lib/app-state";
import {
  developRaw,
  exportSelection,
  getAnalysis,
  getHealth,
  importToPhotos,
  isEndpointUnavailable,
  startAnalyze,
} from "./lib/raw-api";
import type { Adjustments, ExportItem } from "./lib/types";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { SummaryStrip } from "./components/SummaryStrip";
import { AnalysisGate } from "./components/AnalysisGate";
import { GroupList } from "./components/GroupList";
import { OperationPanel } from "./components/OperationPanel";
import { ExportPanel, type ExportRow } from "./components/ExportPanel";

const EXPORT_UNAVAILABLE = "書き出し機能を準備中です。しばらくしてからお試しください。";

// ローカルRAWエンジンは rawpy/numpy/opencv/lensfunpy の import で起動に数秒かかる。
// デスクトップシェルはウィンドウ表示とサイドカー起動をほぼ同時に行うため、初回の
// ヘルスチェックはサーバがまだ listen していない状態で当たりうる（isEndpointUnavailable
// が true になる 404 / 接続不能ケース）。この場合は一定回数だけ再試行し、それでも
// ダメなら（エンジンが本当に起動していない＝ npm run setup:raw 未実施など）既存の
// オフライン表示にフォールバックする。
const HEALTH_CHECK_RETRY_INTERVAL_MS = 1000;
const HEALTH_CHECK_MAX_ATTEMPTS = 20; // 1000ms × 20 ≈ 20秒、冷間起動の実測時間に対して十分な余裕

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export default function Home() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [tick, setTick] = useState(0);
  const comparisonRef = useRef<HTMLDivElement>(null);

  const { analysis, health, screen, developMode } = state;

  // ---- ローカルRAWエンジンの死活確認（起動直後は数秒未起動のため再試行あり） -----
  useEffect(() => {
    let cancelled = false;
    let retryTimer: number | undefined;

    const checkHealth = (attempt: number) => {
      getHealth()
        .then((payload) => {
          if (!cancelled) dispatch({ type: "health/ok", health: payload });
        })
        .catch((error) => {
          if (cancelled) return;
          if (isEndpointUnavailable(error) && attempt < HEALTH_CHECK_MAX_ATTEMPTS) {
            retryTimer = window.setTimeout(() => checkHealth(attempt + 1), HEALTH_CHECK_RETRY_INTERVAL_MS);
            return;
          }
          dispatch({ type: "health/fail" });
        });
    };

    checkHealth(1);

    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, []);

  // ---- 解析状況の取得と 1 秒ポーリング ------------------------------------
  const pollAnalysis = useCallback(async () => {
    try {
      const response = await getAnalysis();
      dispatch({ type: "analysis/status", response });
    } catch (error) {
      dispatch({ type: "analysis/error", message: messageOf(error, "解析状況を取得できませんでした。") });
    }
  }, []);

  useEffect(() => {
    // ローカルRAWエンジンの死活確認（上の useEffect）が終わる前に叩くと、
    // デスクトップシェルではエンジン起動と同時にウィンドウが開くため
    // 同じコールドスタート競合で "Load failed" になる。ready になってから叩く。
    if (state.engineStatus !== "ready") return;
    void pollAnalysis();
  }, [state.engineStatus, pollAnalysis]);

  useEffect(() => {
    if (state.analysisState !== "running") return;
    const timer = window.setInterval(() => void pollAnalysis(), 1000);
    return () => window.clearInterval(timer);
  }, [state.analysisState, pollAnalysis]);

  // ---- 現像の経過秒表示（tick を進めるだけ。経過秒はレンダー時に導出する） ----
  useEffect(() => {
    if (!developMode) return;
    const timer = window.setInterval(() => setTick(Date.now()), 200);
    return () => window.clearInterval(timer);
  }, [developMode]);

  // ---- 派生値 --------------------------------------------------------------
  const group = selectedGroup(state);
  const captureId = group ? selectedCaptureOf(state, group) : "";
  const photo = photoOf(analysis, captureId);
  const adjustments = effectiveAdjustments(state);
  const changedKeys = changedAdjustmentKeys(state);
  const groupsToShow = useMemo(() => visibleGroups(state), [state]);
  const developedIds = useMemo(() => developedGroupIds(state), [state]);
  const engineVersion = health ? `rawpy ${health.rawpy_version}` : "";
  const coverThumb = useMemo(() => {
    if (!analysis) return null;
    const first = analysis.groups[0];
    return first ? (analysis.photos[first.recommended_capture_id]?.thumb ?? null) : null;
  }, [analysis]);

  const exportRows = useMemo<ExportRow[]>(() => {
    if (!analysis) return [];
    return state.acceptedGroupIds
      .map((groupId) => analysis.groups.find((item) => item.id === groupId))
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .map((item) => {
        const chosen = selectedCaptureOf(state, item);
        const developed = state.fullResults[chosen];
        const variant = state.exportVariants[chosen] ?? (developed ? "developed" : "camera_jpg");
        return { group: item, captureId: chosen, developed, variant: developed ? variant : "camera_jpg" };
      });
  }, [analysis, state]);

  const albumName = `GeoFrame RAW Assist ${isValidBatchName(state.batchName) ? state.batchName : "<バッチ名>"}`;

  // ---- 操作ハンドラ --------------------------------------------------------
  function scrollToTop() {
    window.requestAnimationFrame(() => {
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    });
  }

  function openOperationScreen(groupId: number) {
    dispatch({ type: "screen/operation", groupId });
    scrollToTop();
  }

  function returnToSuggestions() {
    dispatch({ type: "screen/suggestions" });
    scrollToTop();
  }

  function openExportScreen() {
    dispatch({ type: "screen/export" });
    scrollToTop();
  }

  async function beginAnalyze(force = false) {
    dispatch({ type: "analysis/requested" });
    try {
      const response = await startAnalyze(force);
      if (response.status === "ready") {
        await pollAnalysis();
      }
    } catch (error) {
      dispatch({ type: "analysis/error", message: messageOf(error, "解析を開始できませんでした。") });
    }
  }

  async function runDevelop(mode: DevelopMode) {
    if (!group || !captureId) return;
    if (state.engineStatus !== "ready") {
      dispatch({ type: "notice/set", message: "ローカルRAWエンジンが停止中です。npm run dev:raw で起動してください", tone: "error" });
      return;
    }
    const preset = activePreset(state);
    if (!preset) {
      dispatch({ type: "notice/set", message: "プリセットを取得できていません。", tone: "error" });
      return;
    }

    dispatch({ type: "develop/start", mode, captureId });
    try {
      const result = await developRaw(captureId, preset.name, {
        adjustments: state.overrides,
        preview: mode === "preview",
        maxEdge: mode === "preview" ? undefined : 6000,
      });
      dispatch({ type: "develop/ok", mode, result });
      window.setTimeout(() => comparisonRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 120);
    } catch (error) {
      dispatch({ type: "develop/fail", message: messageOf(error, "RAW現像に失敗しました") });
    }
  }

  async function runExport() {
    if (!isValidBatchName(state.batchName) || exportRows.length === 0) return;
    dispatch({ type: "export/start" });
    const items: ExportItem[] = exportRows.map((row) =>
      row.variant === "developed" && row.developed
        ? { capture_id: row.captureId, variant: "developed", developed_filename: row.developed.filename }
        : { capture_id: row.captureId, variant: "camera_jpg" },
    );
    try {
      const result = await exportSelection(state.batchName, items);
      dispatch({ type: "export/ok", result });
    } catch (error) {
      dispatch({
        type: "export/fail",
        message: isEndpointUnavailable(error) ? EXPORT_UNAVAILABLE : messageOf(error, "書き出しに失敗しました。"),
      });
    }
  }

  async function runImport() {
    if (!isValidBatchName(state.batchName)) return;
    dispatch({ type: "import/start" });
    try {
      const result = await importToPhotos(state.batchName);
      dispatch({ type: "import/ok", result });
    } catch (error) {
      dispatch({
        type: "import/fail",
        message: isEndpointUnavailable(error) ? EXPORT_UNAVAILABLE : messageOf(error, "Appleフォトへの登録に失敗しました。"),
      });
    }
  }

  const analysisReady = state.analysisState === "ready" && analysis !== null;
  const elapsed = developMode ? Math.max(0, (tick - state.developStartedAt) / 1000) : 0;
  const elapsedLabel = `${elapsed.toFixed(1)}秒`;

  return (
    <div className="app-shell">
      <Sidebar analysis={analysis} coverThumb={coverThumb} />

      <main className="workspace">
        <Topbar
          screen={screen}
          groupTitle={group?.title ?? ""}
          groupKind={group ? group.raw.label : ""}
          captureId={captureId}
          engineStatus={state.engineStatus}
          engineVersion={engineVersion}
          canExport={state.acceptedGroupIds.length > 0}
          acceptedCount={state.acceptedGroupIds.length}
          analysisReady={analysisReady}
          reanalyzing={state.analysisState === "running"}
          onReturn={returnToSuggestions}
          onOpenExport={openExportScreen}
          onReanalyze={() => void beginAnalyze(true)}
        />

        {screen === "suggestions" && analysisReady && analysis && <SummaryStrip analysis={analysis} />}

        {state.notice && (
          <div className={`toast${state.noticeTone === "error" ? " error" : ""}`} role="status">
            <span>{state.noticeTone === "error" ? "!" : "✓"}</span>
            {state.notice}
            <button onClick={() => dispatch({ type: "notice/clear" })} aria-label="閉じる">×</button>
          </div>
        )}

        {!analysisReady || !analysis ? (
          <AnalysisGate
            engineStatus={state.engineStatus}
            analysisState={state.analysisState}
            progress={state.analysisProgress}
            errorMessage={state.analysisError}
            busy={state.analyzeRequested}
            onStart={() => void beginAnalyze(false)}
          />
        ) : screen === "operation" && group ? (
          <OperationPanel
            analysis={analysis}
            group={group}
            photo={photo}
            captureId={captureId}
            onChooseCapture={(groupId, id) => dispatch({ type: "group/chooseCapture", groupId, captureId: id })}
            engineStatus={state.engineStatus}
            engineVersion={engineVersion}
            health={health}
            presetName={state.presetName}
            adjustments={adjustments}
            changedKeys={changedKeys}
            onChangePreset={(presetName) => dispatch({ type: "preset/change", presetName })}
            onChangeAdjustment={(key, value) =>
              dispatch({ type: "adjust/change", key, value: value as Adjustments[keyof Adjustments] })
            }
            onResetAdjustments={() => dispatch({ type: "adjust/reset" })}
            isAccepted={state.acceptedGroupIds.includes(group.id)}
            onAccept={() => dispatch({ type: "group/accept", groupId: group.id })}
            onDevelop={(mode) => void runDevelop(mode)}
            developMode={developMode}
            elapsedLabel={elapsedLabel}
            previewResult={state.previewResults[captureId]}
            fullResult={state.fullResults[captureId]}
            comparisonRef={comparisonRef}
          />
        ) : screen === "export" ? (
          <ExportPanel
            analysis={analysis}
            rows={exportRows}
            batchName={state.batchName}
            onChangeBatchName={(value) => dispatch({ type: "export/batchName", value })}
            onChangeVariant={(id, variant) => dispatch({ type: "export/variant", captureId: id, variant })}
            onExport={() => void runExport()}
            onImport={() => void runImport()}
            exporting={state.exporting}
            importing={state.importing}
            exportResult={state.exportResult}
            importResult={state.importResult}
            albumName={albumName}
            onBack={returnToSuggestions}
          />
        ) : (
          <GroupList
            analysis={analysis}
            visibleGroups={groupsToShow}
            selectedCaptureOf={(item) => selectedCaptureOf(state, item)}
            acceptedGroupIds={state.acceptedGroupIds}
            developedGroupIds={developedIds}
            activeView={state.listView}
            onChangeView={(view) => dispatch({ type: "list/view", view })}
            onOpenGroup={openOperationScreen}
          />
        )}
      </main>
    </div>
  );
}
