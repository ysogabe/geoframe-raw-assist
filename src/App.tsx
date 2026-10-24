import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  acceptedKey,
  acceptedMergeOf,
  acceptedSummaryOf,
  activePreset,
  changedAdjustmentKeys,
  currentDevelopmentCandidatesResponse,
  developedGroupIds,
  effectiveAdjustments,
  effectiveViewingOf,
  heroCaptureOf,
  initialState,
  isPhotoAccepted,
  isValidBatchName,
  isBracketGroup,
  mergeBlockReason,
  currentMergeResultsOf,
  mergeFramesOf,
  mergedAcceptTargetOf,
  mergedResultFor,
  photoOf,
  reducer,
  selectedGroup,
  viewingCaptureOf,
  visibleGroups,
  type DevelopMode,
  type PhotoExportVariant,
} from "./lib/app-state";
import {
  exportSelection,
  importToPhotos,
  getAnalysis,
  getPresets,
  getSourceFolder,
  isSourceFolderApiError,
  isSourceRootNotConfigured,
  setSourceFolder,
  startAnalyze,
} from "./lib/raw-api";
import { SOURCE_FOLDER_NO_PAIRS, sourceFolderErrorMessage } from "./lib/format";
import { adjacentGroupId, isEditableTarget } from "./lib/group-nav";
import { confirmDiscard } from "./lib/confirm";
import { invoke } from "@tauri-apps/api/core";
import type { Adjustments, DevelopmentCandidatesResponse, ExportItem } from "./lib/types";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { SummaryStrip } from "./components/SummaryStrip";
import { AnalysisGate } from "./components/AnalysisGate";
import { GroupList } from "./components/GroupList";
import { GroupStrip } from "./components/GroupStrip";
import { OperationPanel } from "./components/OperationPanel";
import { ExportPanel, type ExportRow } from "./components/ExportPanel";
import { FullSizeViewer } from "./components/FullSizeViewer";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import {
  coreImageErrorMessage,
  developPreviewCoreImage,
  developFullCoreImage,
  mergeBracketCoreImage,
  engineInfoLabel,
  getEngineInfo,
  resolveAdjustments,
  toDevelopedResult,
} from "./lib/coreimage-api";

// ←→ でグループを送ったあと、写真カードの上端を固定グループ帯の下端からどれだけ離すか。
const MEDIA_CARD_GAP = 12;

function messageOf(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  // Tauri の invoke は { code, message } の素のオブジェクトで reject する。
  const message = (error as { message?: unknown } | null)?.message;
  return typeof message === "string" && message ? message : fallback;
}

/** エンジン停止時の案内。 */
const ENGINE_STOPPED_MESSAGE = "現像エンジンを利用できません。アプリを再起動してください";

/** set_source_folder の失敗を、reason 別の案内文に落とす。 */
function sourceFolderFailureMessage(error: unknown): string {
  if (isSourceFolderApiError(error)) return sourceFolderErrorMessage(error.reason, error.message);
  return messageOf(error, "現像対象フォルダを変更できませんでした。");
}

export default function Home() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [tick, setTick] = useState(0);
  const comparisonRef = useRef<HTMLDivElement>(null);

  const { analysis, screen, developMode } = state;
  // 書き出し行が読んでよい state はこの 3 つだけ。表示カーソル（viewing）を混ぜないよう
  // 分解して useMemo の依存に並べる（state 全体を依存にすると、読んでいる範囲が隠れる）。
  const { accepted, fullResults, mergeFullResults } = state;

  // ---- エンジン確認。engineStatus は engine_info（Rust）だけで決める（#109） -----
  useEffect(() => {
    let cancelled = false;
    getEngineInfo()
      .then((info) => {
        if (!cancelled) dispatch({ type: "engine/ok", label: engineInfoLabel(info) });
      })
      .catch(() => {
        if (!cancelled) dispatch({ type: "engine/fail" });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- プリセット一覧（#98）。エンジン確認と独立に、起動時に 1 回だけ取得する --------------
  // getPresets() は取得失敗・空のとき [] を返すので、そのときは何も出さない。
  useEffect(() => {
    let cancelled = false;
    void getPresets().then((presets) => {
      if (!cancelled && presets.length > 0) dispatch({ type: "presets/loaded", presets });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- 解析状況の取得と 1 秒ポーリング ------------------------------------
  const pollAnalysis = useCallback(async () => {
    try {
      const response = await getAnalysis();
      dispatch({ type: "analysis/status", response });
    } catch (error) {
      if (isSourceRootNotConfigured(error)) {
        dispatch({ type: "analysis/unconfigured" });
        return;
      }
      dispatch({ type: "analysis/error", message: messageOf(error, "解析状況を取得できませんでした。") });
    }
  }, []);

  useEffect(() => {
    // エンジン確認（上の useEffect）が終わってから叩く。
    if (state.engineStatus !== "ready") return;
    void pollAnalysis();
  }, [state.engineStatus, pollAnalysis]);

  useEffect(() => {
    if (state.analysisState !== "running") return;
    const timer = window.setInterval(() => void pollAnalysis(), 1000);
    return () => window.clearInterval(timer);
  }, [state.analysisState, pollAnalysis]);

  // ---- 現像対象フォルダの取得 ----------------------------------------------
  // 解析状況と同じくエンジンが ready になってから取りに行く。
  useEffect(() => {
    if (state.engineStatus !== "ready") return;
    let cancelled = false;

    void (async () => {
      try {
        const info = await getSourceFolder();
        if (!cancelled) dispatch({ type: "source/loaded", info });
      } catch (error) {
        if (cancelled) return;
        dispatch({
          type: "source/failed",
          message: messageOf(error, "現像対象フォルダを取得できませんでした。"),
        });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state.engineStatus]);

  // ---- 現像の経過秒表示（tick を進めるだけ。経過秒はレンダー時に導出する） ----
  useEffect(() => {
    if (!developMode && !state.mergeMode) return;
    const timer = window.setInterval(() => setTick(Date.now()), 200);
    return () => window.clearInterval(timer);
  }, [developMode, state.mergeMode]);

  // ---- 派生値 --------------------------------------------------------------
  const group = selectedGroup(state);
  // 表示カーソルは 1 本。合成を見ている間は「今の写真」も合成の先頭フレームになる。
  // 画面に実際に出している面（合う合成結果が無ければ merged カーソルでも写真面）。
  const viewing = group ? effectiveViewingOf(state, group) : null;
  const captureId = group ? viewingCaptureOf(state, group) : "";
  const adjustments = effectiveAdjustments(state);
  const mergeFrames = group ? mergeFramesOf(state, group) : [];
  // 画面に出す合成結果は「今の選択・プリセット・調整に合うもの」だけ。表示、SELECTED PHOTO、
  // 写真の採用先（captureId）は同じ currentMergeResultsOf から決まる。
  const currentMerge = group ? currentMergeResultsOf(state, group) : {};
  const mergePreviewResult = currentMerge.preview;
  const mergeFullResult = currentMerge.full;
  // 採用済みは採用時のスナップショットで見る（現像パネルを触っただけで採用が外れたようには
  // 見せない）。ただし ✓ は **今表示している合成が採用そのもの** のときだけ。今の設定に
  // 合わなくなった採用は別に「採用済み（以前の設定）」として扱い、今の合成へ移さない。
  const acceptedMerge = group ? acceptedMergeOf(state, group) : undefined;
  const mergedAccepted = Boolean(acceptedMerge?.isCurrent);
  const previousAcceptedMerge = acceptedMerge && !acceptedMerge.isCurrent ? acceptedMerge : null;
  const groupMergeMode = group?.id === state.mergingGroupId ? state.mergeMode : null;
  const globalBusy = Boolean(state.developMode || state.mergeMode || state.developmentCandidatesBusy);
  const photo = photoOf(analysis, captureId);
  const changedKeys = changedAdjustmentKeys(state);
  const groupsToShow = useMemo(() => visibleGroups(state), [state]);
  const developedIds = useMemo(() => developedGroupIds(state), [state]);
  const engineVersion = state.engineLabel ?? "";
  const coverThumb = useMemo(() => {
    if (!analysis) return null;
    const first = analysis.groups[0];
    return first ? (analysis.photos[first.recommended_capture_id]?.thumb ?? null) : null;
  }, [analysis]);

  /**
   * 書き出す行。**採用アイテムだけから作る**。
   *
   * ここが今回の修正の中核。以前は採用がグループ単位のフラグでしかなく、書き出す
   * 写真を「今の代表」から遅延解決していたため、採用したあとに代表を変えると
   * 書き出される写真が無言で変わっていた。表示カーソル（state.viewing）は
   * **絶対に参照しない**こと。
   *
   * 一方で成果物（DevelopedResult / MergedResult）はここで引き直す。採用時に
   * filename を握ると、本現像をやり直したあとも古いファイルを書き出してしまう
   * （outputs/ には params_hash 違いが残るので、成功するぶん気づけない）。
   */
  const exportRows = useMemo<ExportRow[]>(() => {
    if (!analysis) return [];
    return accepted.flatMap<ExportRow>((item) => {
      const group = analysis.groups.find((candidate) => candidate.id === item.groupId);
      // 再解析の結び直しで引けなかったアイテムは reducer 側で落としているので、
      // ここに来るのは基本的に解析の入れ替わりと表示の間の一瞬だけ。
      if (!group) return [];
      switch (item.kind) {
        case "photo": {
          const developed = fullResults[item.captureId];
          // 本現像の結果が無いのに "developed" のままの行は必ずカメラJPGへ落とす
          // （結果を消してから書き出すと 400 になるため）。
          const variant: PhotoExportVariant =
            item.variant === "developed" && !developed ? "camera_jpg" : item.variant;
          return [
            { kind: "photo", key: acceptedKey(item), group, captureId: item.captureId, developed, variant },
          ];
        }
        case "merged":
          return [
            {
              kind: "merged",
              key: acceptedKey(item),
              group,
              captureId: item.leadCaptureId,
              // グループの「現在の」合成だけを見る。全件スキャンだと、IDが使い回されて
              // 同じ先頭フレームの古い合成が別キーに残っているとき、そちらを拾ってしまう。
              merged: mergedResultFor(mergeFullResults, group, item.leadCaptureId),
            },
          ];
      }
    });
  }, [analysis, accepted, fullResults, mergeFullResults]);

  // ---- 操作ハンドラ --------------------------------------------------------
  function scrollToTop() {
    window.requestAnimationFrame(() => {
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    });
  }

  /**
   * 大きい写真カード（.operation-media-card）の上端が、固定されたグループ帯の直下に
   * 来る位置へ送る。←→ で次々送るとき、毎回ページ先頭へ戻されると写真を見るたびに
   * スクロールし直すことになるため。
   *
   * 帯の高さは「今の」実測値で引く。ページ先頭付近（帯が通常表示）から送ると、
   * スクロール途中で帯がコンパクト化して縮むが、縮んだぶん下の要素も同じだけ上へ
   * 詰まるので、着地時の写真カードは結局コンパクト帯の直下に来る。
   */
  function scrollToMediaCard() {
    window.requestAnimationFrame(() => {
      const card = document.querySelector<HTMLElement>(".operation-media-card");
      if (!card) return;
      const stripHeight = document.querySelector<HTMLElement>(".group-strip")?.offsetHeight ?? 0;
      const top = card.getBoundingClientRect().top + window.scrollY - stripHeight - MEDIA_CARD_GAP;
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.scrollTo({ top: Math.max(0, top), behavior: reduceMotion ? "auto" : "smooth" });
    });
  }

  /** グループ移動の確認ダイアログ表示中。二重に出さないための再入ガード。 */
  const confirmingRef = useRef(false);
  /** 最新レンダーの選択グループ id（確認ダイアログの await 後に古い closure の group を見ないため）。 */
  const currentGroupIdRef = useRef<number | null>(null);

  function openOperationScreen(groupId: number) {
    dispatch({ type: "screen/operation", groupId });
    scrollToTop();
  }

  /**
   * 操作パネル内でのグループ移動（グループ帯のクリック・◀▶・←→ キー）の共通入口。
   *
   * screen/operation は「移動」の意味で presetName / overrides をリセットするので、
   * 手動調整が残っているときは黙って捨てずに確認を取る。選択中のグループへの
   * 「移動」は何もしない（再ディスパッチで調整が消えるのを防ぐ。GroupStrip 側の
   * 再クリック無視と同じ理由）。
   *
   * 確認は非同期（Tauri ではネイティブダイアログ。WKWebView は window.confirm を
   * 出さない）。表示中の再入は confirmingRef で弾き、確認中に選択グループが
   * 変わっていたら（最新値は currentGroupIdRef で見る）移動しない。
   * 呼び出し側は戻り値の Promise を待たなくてよい（例外は内部で処理する）。
   */
  async function moveToGroup(groupId: number, scroll: "top" | "media") {
    if (!group || groupId === group.id) return;
    if (confirmingRef.current) return;
    if (changedKeys.length > 0) {
      const fromGroupId = group.id;
      confirmingRef.current = true;
      let ok: boolean;
      try {
        ok = await confirmDiscard("調整値が失われます。移動しますか？");
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        dispatch({ type: "notice/set", message: `移動の確認ダイアログを表示できませんでした: ${detail}`, tone: "error" });
        return;
      } finally {
        confirmingRef.current = false;
      }
      if (!ok || currentGroupIdRef.current !== fromGroupId) return;
    }
    dispatch({ type: "screen/operation", groupId });
    if (scroll === "media") scrollToMediaCard();
    else scrollToTop();
  }

  /** 操作パネルでの ←→ キー。analysis.groups の並び順で前後のグループへ送る。 */
  function handleGroupKey(event: KeyboardEvent) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    // 誰かが既に処理したキー・修飾キー付き（ブラウザ/OS のショートカット）・IME 変換中は触らない。
    if (event.defaultPrevented || event.isComposing) return;
    if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    // スライダーやテキスト入力は矢印キーを自分で使う。
    if (isEditableTarget(event.target)) return;
    // 原寸ビューアは自前で ←→ を写真送りに使う（<dialog> 内の keydown が window まで上がってくる）。
    if (state.viewer && state.viewer.items.length > 0) return;
    if (!analysis || !group) return;
    const next = adjacentGroupId(analysis.groups, group.id, event.key === "ArrowLeft" ? -1 : 1);
    // 両端では何もしない（既定動作も奪わない）。
    if (next === null) return;
    // 既定動作の抑止は同期のうちに（確認ダイアログの await 後では効かない）。
    event.preventDefault();
    void moveToGroup(next, "media");
  }

  // window の購読は操作画面の間だけ張り替えずに持ち、中身は毎レンダーの最新ハンドラへ
  // 委ねる（state を閉じ込めたハンドラを直接登録すると古い group / changedKeys を見る）。
  const groupKeyHandlerRef = useRef(handleGroupKey);
  useEffect(() => {
    groupKeyHandlerRef.current = handleGroupKey;
    currentGroupIdRef.current = group?.id ?? null;
  });
  useEffect(() => {
    if (screen !== "operation") return;
    const listener = (event: KeyboardEvent) => groupKeyHandlerRef.current(event);
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [screen]);

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
      if (isSourceRootNotConfigured(error)) {
        dispatch({ type: "analysis/unconfigured" });
        return;
      }
      dispatch({ type: "analysis/error", message: messageOf(error, "解析を開始できませんでした。") });
    }
  }

  /** 「フォルダ未設定」画面から、SourceFolderPanel と同じネイティブダイアログで現像元を選ぶ。 */
  async function pickFolderForGate() {
    try {
      const selected = await openDialog({ directory: true, multiple: false });
      if (typeof selected !== "string" || !selected) return;
      await applySourceFolder(selected);
    } catch (error) {
      dispatch({
        type: "notice/set",
        message: messageOf(error, "フォルダ選択ダイアログを開けませんでした。"),
        tone: "error",
      });
    }
  }

  async function applySourceFolder(path: string) {
    const trimmed = path.trim();
    if (!trimmed || state.sourceFolderBusy) return;
    dispatch({ type: "source/applying" });
    try {
      const info = await setSourceFolder(trimmed);
      // capture_id に紐づく状態（選択グループ・現像結果・書き出し選択）を捨ててから、
      // 新しいフォルダを解析し直す。前のフォルダの DSC02943 をそのまま持ち越すと
      // 新しいフォルダの別写真を指してしまうため、リセットは必須。
      const noPairs = info.pairs === 0;
      dispatch({
        type: "source/changed",
        info,
        message: noPairs
          ? SOURCE_FOLDER_NO_PAIRS
          : `現像対象フォルダを ${info.display_path} に変更しました。解析をやり直します。`,
        tone: noPairs ? "error" : "info",
      });
      if (noPairs) {
        // ペアが1組も無いフォルダで解析を走らせても得るものが無いうえ、解析中は
        // フォルダ変更をロックするため「別のフォルダを選び直してください」と案内
        // しながら選び直せない状態になってしまう。フォルダ変更時点で エンジン側の
        // 解析キャッシュは無効化済み（get_analysis は absent に戻る）なので、
        // 状態を取り直すだけにして、再解析は AnalysisGate の「解析を開始」に委ねる。
        await pollAnalysis();
        return;
      }
      await beginAnalyze(true);
    } catch (error) {
      dispatch({ type: "source/failed", message: sourceFolderFailureMessage(error) });
    }
  }

  async function runDevelop(mode: DevelopMode) {
    if (!group || !captureId || state.developmentCandidatesBusy || state.developMode || state.mergeMode) return;
    if (state.engineStatus !== "ready") {
      dispatch({ type: "notice/set", message: ENGINE_STOPPED_MESSAGE, tone: "error" });
      return;
    }
    const preset = activePreset(state);
    if (!preset) {
      dispatch({ type: "notice/set", message: "プリセットを取得できていません。", tone: "error" });
      return;
    }

    // 現在のフォルダの世代を **await の前に** 控える。await を跨ぐと state は古い
    // クロージャのままなので dispatch 時点で読み直しても意味がなく、そもそも「この
    // リクエストを出したときのフォルダ」を記録するのが目的。
    const generation = state.sourceFolderGeneration;

    dispatch({ type: "develop/start", mode, captureId });
    const scrollToComparison = () =>
      window.setTimeout(() => comparisonRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 120);

    try {
      const adjustments = resolveAdjustments(preset, state.overrides);
      const result = mode === "preview"
        ? toDevelopedResult(await developPreviewCoreImage(captureId, adjustments), preset)
        : await developFullCoreImage(captureId, adjustments, preset);
      dispatch({ type: "develop/ok", mode, result, generation });
      scrollToComparison();
    } catch (error) {
      dispatch({ type: "develop/fail", message: coreImageErrorMessage(error) });
    }
  }

  async function runDevelopmentCandidates() {
    if (!group || !captureId || state.developmentCandidatesBusy || state.developMode || state.mergeMode) return;
    if (state.engineStatus !== "ready") {
      dispatch({ type: "notice/set", message: ENGINE_STOPPED_MESSAGE, tone: "error" });
      return;
    }
    const preset = activePreset(state);
    if (!preset) return;
    const generation = state.sourceFolderGeneration;
    dispatch({ type: "candidates/start", captureId, generation });
    try {
      const response = await invoke<DevelopmentCandidatesResponse>("generate_development_candidates", {
        captureId, preset: preset.name,
      });
      dispatch({ type: "candidates/ok", captureId, response, generation });
    } catch (error) {
      dispatch({ type: "candidates/fail", captureId, generation,
        message: messageOf(error, "現像候補の生成に失敗しました。") });
    }
  }

  async function runMerge(mode: DevelopMode) {
    if (!group || !isBracketGroup(group) || state.developmentCandidatesBusy || state.developMode || state.mergeMode) return;
    if (state.engineStatus !== "ready") {
      dispatch({ type: "notice/set", message: ENGINE_STOPPED_MESSAGE, tone: "error" });
      return;
    }
    const preset = activePreset(state);
    if (!preset) {
      dispatch({ type: "notice/set", message: "プリセットを取得できていません。", tone: "error" });
      return;
    }
    const frames = mergeFrames.slice();
    const blocked = mergeBlockReason(frames);
    if (blocked) {
      dispatch({ type: "notice/set", message: blocked, tone: "error" });
      return;
    }
    const generation = state.sourceFolderGeneration;
    const groupId = group.id;
    dispatch({ type: "merge/start", mode, groupId });
    try {
      const result = await mergeBracketCoreImage(frames, resolveAdjustments(preset, state.overrides), preset, mode);
      dispatch({ type: "merge/ok", mode, groupId, result, generation });
      window.setTimeout(() => comparisonRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 120);
    } catch (error) {
      dispatch({ type: "merge/fail", groupId, generation, message: coreImageErrorMessage(error) });
    }
  }

  async function runExport() {
    if (!isValidBatchName(state.batchName) || exportRows.length === 0) return;
    const items: ExportItem[] = exportRows.flatMap<ExportItem>((row) => {
      switch (row.kind) {
        case "merged":
          // 成果物が失われた行は送れない（書き出し画面には「合成結果なし」として残る）。
          if (!row.merged) return [];
          // capture_id は合成ファイル名の先頭IDでなければサーバが 400 を返す。
          return [
            { capture_id: row.merged.capture_ids[0], variant: "merged", merged_filename: row.merged.filename },
          ];
        case "photo":
          if (row.variant === "developed" && row.developed) {
            return [
              { capture_id: row.captureId, variant: "developed", developed_filename: row.developed.filename },
            ];
          }
          return [{ capture_id: row.captureId, variant: "camera_jpg" }];
      }
    });
    // 成果物の無い行しか無いと、空のリクエストを投げて「0件を書き出しました」になる。
    // 何が起きたか分からない成功より、押した理由を否定して理由を出すほうがよい。
    if (items.length === 0) {
      dispatch({
        type: "notice/set",
        message: "採用したものの成果物が見つかりません。合成し直して採用し直してください。",
        tone: "error",
      });
      return;
    }
    // 一部だけ成果物が失われている場合（例: 採用3件のうち1件が合成し直しで孤児化）、
    // その行は上の flatMap で落ちている。全滅（items.length === 0）だけを止めていると、
    // 残りが書き出せてしまうぶん「成功」として通知され、欠けたことに気づけない。
    // 何件落としたかを数えて通知に載せる。
    const missingCount = exportRows.length - items.length;
    dispatch({ type: "export/start" });
    try {
      const result = await exportSelection(state.batchName, items);
      dispatch({ type: "export/ok", result, missingCount });
    } catch (error) {
      dispatch({
        type: "export/fail",
        message: messageOf(error, "書き出しに失敗しました。"),
      });
    }
  }

  async function runImport() {
    if (!isValidBatchName(state.batchName) || !state.exportResult) return;
    dispatch({ type: "import/start" });
    try {
      const result = await importToPhotos({ batch_name: state.batchName });
      dispatch({ type: "import/ok", result });
    } catch (error) {
      dispatch({ type: "import/fail", message: messageOf(error, "Appleフォトへの登録に失敗しました。") });
    }
  }

  const analysisReady = state.analysisState === "ready" && analysis !== null;
  const elapsed = developMode ? Math.max(0, (tick - state.developStartedAt) / 1000) : 0;
  const elapsedLabel = `${elapsed.toFixed(1)}秒`;

  return (
    <div className="app-shell">
      <Sidebar
        analysis={analysis}
        coverThumb={coverThumb}
        sourceFolder={state.sourceFolder}
        sourceFolderBusy={state.sourceFolderBusy}
        // 解析中だけでなく、現像が走っている間もフォルダ切替を止める。切り替えると
        // 飛んでいるリクエストの結果が前のフォルダのものになり、capture_id の衝突で
        // 新しいフォルダの写真に化ける（runMerge の多重起動ガードと同じ考え方）。
        // なお取りこぼしの窓は残るため、reducer 側の sourceFolderGeneration が最終防衛線。
        sourceFolderLocked={
          state.analysisState === "running" || globalBusy
        }
        onApplySourceFolder={(path) => void applySourceFolder(path)}
        onSourceFolderError={(message) => dispatch({ type: "source/failed", message })}
      />

      <main className="workspace">
        {/* 要素構造は画面によらず同一にし、クラス名だけを切り替える。三項でラッパーの
            有無を切り替えると Topbar が remount される。 */}
        <div className={screen === "suggestions" ? "page-header page-header-sticky" : "page-header"}>
          <Topbar
            screen={screen}
            groupTitle={group?.title ?? ""}
            groupKind={group ? group.raw.label : ""}
            captureId={captureId}
            engineStatus={state.engineStatus}
            engineVersion={engineVersion}
            canExport={accepted.length > 0}
            acceptedCount={accepted.length}
            analysisReady={analysisReady}
            reanalyzing={state.analysisState === "running"}
            onReturn={returnToSuggestions}
            onOpenExport={openExportScreen}
            onReanalyze={() => void beginAnalyze(true)}
          />

          {screen === "suggestions" && analysisReady && analysis && <SummaryStrip analysis={analysis} />}
        </div>

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
            onPickFolder={() => void pickFolderForGate()}
          />
        ) : screen === "operation" && group && viewing ? (
          <>
            {/* 一覧へ戻らずに次のグループへ移るための帯。フィルタタブの状態は反映せず、
                常に全グループを渡す（絞り込みの外へ移動できなくなるため）。 */}
            <GroupStrip
              analysis={analysis}
              groups={analysis.groups}
              selectedGroupId={group.id}
              heroCaptureOf={(item) => heroCaptureOf(state, item)}
              acceptedSummaryOf={(item) => acceptedSummaryOf(state, item.id)}
              // クリック・◀▶ の移動も ←→ キーと同じ確認を通す。カードのクリックは従来どおり
              // ページ先頭へ、◀▶ は ←→ キーと同じ操作なので着地もキーと揃える。
              onSelectGroup={(groupId) => void moveToGroup(groupId, "top")}
              onStepGroup={(groupId) => void moveToGroup(groupId, "media")}
            />
            <OperationPanel
              analysis={analysis}
              group={group}
              photo={photo}
              captureId={captureId}
              onViewCapture={(groupId, id) => dispatch({ type: "view/capture", groupId, captureId: id })}
              engineStatus={state.engineStatus}
              engineVersion={engineVersion}
              presets={state.presets}
              presetName={state.presetName}
              adjustments={adjustments}
              changedKeys={changedKeys}
              onChangePreset={(presetName) => dispatch({ type: "preset/change", presetName })}
              onChangeAdjustment={(key, value) =>
                dispatch({ type: "adjust/change", key, value: value as Adjustments[keyof Adjustments] })
              }
              onResetAdjustments={() => dispatch({ type: "adjust/reset" })}
              isPhotoAccepted={(id) => isPhotoAccepted(state, id)}
              onAcceptPhoto={(id) =>
                dispatch({
                  type: "accept/photo",
                  groupId: group.id,
                  // 宛先は **引数の撮影ID**。フィルムストリップは表示中でない写真も採用できるので、
                  // ここで表示カーソル（captureId）を使うと別の写真の採用として記録される。
                  captureId: id,
                  // 採用した時点で本現像があるならそれを既定にする。以後は行の選択が正で、
                  // あとから本現像しても勝手に昇格させない（無言で書き出す物を変えないため）。
                  variant: state.fullResults[id] ? "developed" : "camera_jpg",
                })
              }
              candidatesResponse={currentDevelopmentCandidatesResponse(state, captureId)}
              candidatesBusy={state.developmentCandidatesBusy}
              candidatesError={state.developmentCandidatesError}
              onGenerateCandidates={() => void runDevelopmentCandidates()}
              onApplyCandidate={(candidate) => dispatch({ type: "candidates/apply", candidate, captureId })}
              isBracket={isBracketGroup(group)}
              mergeFrames={mergeFrames}
              onToggleMergeFrame={(id) => dispatch({ type: "merge/toggleFrame", groupId: group.id, captureId: id })}
              onResetMergeFrames={() => dispatch({ type: "merge/resetFrames", groupId: group.id })}
              onMerge={(mode) => void runMerge(mode)}
              mergeMode={groupMergeMode}
              mergeElapsedLabel={`${Math.max(0, (tick - state.mergeStartedAt) / 1000).toFixed(1)}秒`}
              mergeBlockReason={mergeBlockReason(mergeFrames)}
              mergePreviewResult={mergePreviewResult}
              mergeFullResult={mergeFullResult}
              mergedAccepted={mergedAccepted}
              previousAcceptedMerge={previousAcceptedMerge}
              onAcceptMerged={() => {
                // 宛先は今の設定に合う本合成だけ（無ければ何もしない）。
                const leadCaptureId = mergedAcceptTargetOf(state, group);
                if (leadCaptureId) dispatch({ type: "accept/merged", groupId: group.id, leadCaptureId });
              }}
              onRemovePreviousAcceptedMerge={() => {
                if (previousAcceptedMerge) {
                  dispatch({ type: "accept/merged", groupId: group.id, leadCaptureId: previousAcceptedMerge.leadCaptureId });
                }
              }}
              onViewMerged={() => dispatch({ type: "view/merged", groupId: group.id })}
              globalBusy={globalBusy}
              onDevelop={(mode) => void runDevelop(mode)}
              developMode={developMode}
              elapsedLabel={elapsedLabel}
              previewResult={state.previewResults[captureId]}
              fullResult={state.fullResults[captureId]}
              previewResults={state.previewResults}
              fullResults={state.fullResults}
              comparisonRef={comparisonRef}
              viewing={viewing}
              onOpenViewer={(gallery) => dispatch({ type: "viewer/open", gallery })}
            />
          </>
        ) : screen === "export" ? (
          <ExportPanel
            analysis={analysis}
            rows={exportRows}
            batchName={state.batchName}
            onChangeBatchName={(value) => dispatch({ type: "export/batchName", value })}
            onChangeVariant={(key, variant) => dispatch({ type: "accept/variant", key, variant })}
            onExport={() => void runExport()}
            onImport={() => void runImport()}
            exporting={state.exporting}
            importing={state.importing}
            exportResult={state.exportResult}
            importResult={state.importResult}
            onBack={returnToSuggestions}
          />
        ) : (
          <GroupList
            analysis={analysis}
            visibleGroups={groupsToShow}
            heroCaptureOf={(item) => heroCaptureOf(state, item)}
            recommendedCaptureOf={(item) => item.recommended_capture_id}
            acceptedSummaryOf={(item) => acceptedSummaryOf(state, item.id)}
            acceptedItemCount={accepted.length}
            developedGroupIds={developedIds}
            activeView={state.listView}
            onChangeView={(view) => dispatch({ type: "list/view", view })}
            onOpenGroup={openOperationScreen}
          />
        )}
      </main>

      {/* 原寸ビューア。showModal() でトップレイヤーに乗るので DOM 上の位置は問わない。
          開くたびに新しく mount して、拡大率と面の選択を前回から持ち越さない。
          ビューア内で前後の写真へ送っても selectedGroupId / viewing は動かさないので、
          閉じたときは開く前の操作パネルがそのまま残る。 */}
      {state.viewer && state.viewer.items.length > 0 && (
        <FullSizeViewer gallery={state.viewer} onClose={() => dispatch({ type: "viewer/close" })} />
      )}
    </div>
  );
}
