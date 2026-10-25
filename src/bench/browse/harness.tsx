/**
 * 閲覧ベンチ専用の描画ハーネス。現行の GroupList / GroupStrip と実 reducer・selector をそのまま使い、
 * App.tsx の他の部分(OperationPanel・Tauri 呼び出し・キー入力)は載せない。
 * **プロダクトのどこからも import されない**(dev/bench の独立エントリ `browse-bench.html` 専用)。
 */
import { Profiler, useCallback, useLayoutEffect, useMemo, useReducer, useRef, useState, type ProfilerOnRenderCallback } from "react";
import { flushSync } from "react-dom";
import { GroupList } from "../../components/GroupList";
import { GroupStrip } from "../../components/GroupStrip";
import { SummaryStrip } from "../../components/SummaryStrip";
import {
  acceptedSummaryIndex,
  developedGroupIds,
  heroCaptureIdsOf,
  initialState,
  reducer,
  visibleGroups,
  type Action,
  type ListView,
  type State,
} from "../../lib/app-state";
import type { Analysis } from "../../lib/types";

export type BenchAction = Action | { type: "__bench/reset" };

/** ドライバが外から React を操作するための口。 */
export type BenchControls = {
  /** flushSync 済み。呼び出しが返った時点でコミットされている。 */
  dispatch(action: BenchAction): void;
  setListMounted(mounted: boolean): void;
  getState(): State;
  root(): HTMLElement | null;
};

type Props = {
  analysis: Analysis;
  onRender: ProfilerOnRenderCallback;
  onControls(controls: BenchControls): void;
  /** 最初のコミット直後(useLayoutEffect)に 1 度だけ呼ぶ。 */
  onFirstCommit(): void;
};

function readyStateOf(analysis: Analysis): State {
  return reducer(initialState, {
    type: "analysis/status",
    response: { status: "ready", analysis },
    generation: initialState.sourceFolderGeneration,
  });
}

export function BenchApp({ analysis, onRender, onControls, onFirstCommit }: Props) {
  const [state, rawDispatch] = useReducer(
    (current: State, action: BenchAction) => (action.type === "__bench/reset" ? readyStateOf(analysis) : reducer(current, action)),
    analysis,
    readyStateOf,
  );
  const [listMounted, setListMounted] = useState(true);
  const rootRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef(state);

  // ref への代入は描画中ではなくコミット後に行う(ルール: render 中に ref を書かない)。
  useLayoutEffect(() => {
    stateRef.current = state;
  });

  useLayoutEffect(() => {
    onControls({
      dispatch: (action) => flushSync(() => rawDispatch(action)),
      setListMounted: (mounted) => flushSync(() => setListMounted(mounted)),
      getState: () => stateRef.current,
      root: () => rootRef.current,
    });
    onFirstCommit();
    // マウント時に 1 度だけ。onControls / onFirstCommit は呼び出し側で安定。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 実 App.tsx と同じ依存(読むスライスだけ)。ここを state 全体に戻すとベンチが実アプリと別物になる。
  const { accepted, fullResults, mergeFullResults, listView, viewing, mergeSelections, mergePreviewResults } = state;
  const { selectedGroupId, presets, presetName, overrides } = state;
  const groupsToShow = useMemo(
    () => visibleGroups({ analysis: state.analysis, listView, accepted, fullResults, mergeFullResults }),
    [state.analysis, listView, accepted, fullResults, mergeFullResults],
  );
  const developedIds = useMemo(
    () => developedGroupIds({ analysis: state.analysis, fullResults, mergeFullResults }),
    [state.analysis, fullResults, mergeFullResults],
  );
  const acceptedSummaries = useMemo(
    () => acceptedSummaryIndex(state.analysis, accepted, mergeFullResults),
    [state.analysis, accepted, mergeFullResults],
  );
  const heroCaptureIds = useMemo(
    () =>
      heroCaptureIdsOf(
        state.analysis?.groups ?? [],
        { viewing, mergeSelections, mergePreviewResults, mergeFullResults, selectedGroupId, presets, presetName, overrides },
        acceptedSummaries,
      ),
    [state.analysis, viewing, mergeSelections, mergePreviewResults, mergeFullResults, selectedGroupId, presets, presetName, overrides, acceptedSummaries],
  );
  const changeView = useCallback((view: ListView) => rawDispatch({ type: "list/view", view }), []);
  const openGroup = useCallback((groupId: number) => rawDispatch({ type: "screen/operation", groupId }), []);

  const screen = state.screen === "operation" && state.selectedGroupId !== null ? "operation" : "suggestions";
  const selectedId = state.selectedGroupId;

  return (
    <Profiler id="root" onRender={onRender}>
      <div className="app-shell" ref={rootRef} data-bench-screen={screen}>
        <aside className="sidebar" aria-hidden="true" />
        <main className="workspace">
          {/* Topbar の場所だけ確保する(最小高さ 112px)。実 Topbar は props が多く、計測対象でもない。 */}
          <div className={screen === "suggestions" ? "page-header page-header-sticky" : "page-header"}>
            <div style={{ minHeight: 112 }} aria-hidden="true" />
            {screen === "suggestions" && <SummaryStrip analysis={analysis} />}
          </div>

          {screen === "operation" && selectedId !== null ? (
            <>
              <Profiler id="strip" onRender={onRender}>
                <GroupStrip
                  analysis={analysis}
                  groups={analysis.groups}
                  selectedGroupId={selectedId}
                  heroCaptureIds={heroCaptureIds}
                  acceptedSummaries={acceptedSummaries}
                  onSelectGroup={openGroup}
                  onStepGroup={openGroup}
                />
              </Profiler>
              {/* OperationPanel の代わり。帯が sticky/コンパクトに切り替わる縦の長さだけ用意する。 */}
              <div style={{ height: 2400 }} aria-hidden="true" />
            </>
          ) : (
            listMounted && (
              <Profiler id="list" onRender={onRender}>
                <GroupList
                  analysis={analysis}
                  visibleGroups={groupsToShow}
                  heroCaptureIds={heroCaptureIds}
                  acceptedSummaries={acceptedSummaries}
                  acceptedItemCount={accepted.length}
                  developedGroupIds={developedIds}
                  activeView={state.listView}
                  onChangeView={changeView}
                  onOpenGroup={openGroup}
                />
              </Profiler>
            )
          )}
        </main>
      </div>
    </Profiler>
  );
}
