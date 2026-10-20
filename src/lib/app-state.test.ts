import { describe, expect, it } from "vitest";
import {
  currentDevelopmentCandidates,
  currentDevelopmentCandidatesResponse,
  initialState,
  reducer,
  type Action,
  type State,
} from "./app-state";
import type {
  Adjustments,
  Analysis,
  DevelopedResult,
  DevelopmentCandidate,
  DevelopmentCandidatesResponse,
  GroupInfo,
  HealthResponse,
  MergedResult,
  PresetInfo,
  SourceFolderInfo,
} from "./types";

// ---------------------------------------------------------------------------
// フィクスチャ
// ---------------------------------------------------------------------------

const BASE_ADJ: Adjustments = {
  exposure: 0,
  wb_temp: 0,
  shadow_lift: 10,
  highlight_recovery: 20,
  brightness: 1,
  contrast: 1,
  saturation: 1,
  sharpness: 1,
  noise_reduction: "off",
} as unknown as Adjustments;

function preset(name: string, adjustments: Partial<Adjustments> = {}): PresetInfo {
  return { name, label: name, description: "", adjustments: { ...BASE_ADJ, ...adjustments } };
}

const HEALTH: HealthResponse = {
  status: "ready",
  engine: "rawpy/LibRaw",
  rawpy_version: "0",
  lensfun: { available: false },
  presets: [
    preset("neutral_recovery"),
    preset("shadow_rescue", { shadow_lift: 60 }),
    preset("landscape_clear"),
  ],
};

function group(id: number, captureIds: string[], overrides: Partial<GroupInfo> = {}): GroupInfo {
  return {
    id,
    kind: "burst",
    title: `g${id}`,
    start_at: "",
    end_at: "",
    capture_ids: captureIds,
    recommended_capture_id: captureIds[0],
    confidence: 1,
    recommendation_reasons: [],
    raw: { level: "maybe", label: "", reason_codes: [], reasons: [] },
    ...overrides,
  } as GroupInfo;
}

function analysisOf(groups: GroupInfo[]): Analysis {
  return { generated_at: "t0", groups, photos: {} } as unknown as Analysis;
}

function candidate(id: string, adjustments: Partial<Adjustments> = {}): DevelopmentCandidate {
  return {
    id,
    label: `label-${id}`,
    description: "",
    adjustments: { ...BASE_ADJ, ...adjustments },
    metrics: {},
    score: 1,
    reasons: [],
  } as unknown as DevelopmentCandidate;
}

function candidatesResponse(
  captureId: string,
  presetName: string,
  candidates: DevelopmentCandidate[],
): DevelopmentCandidatesResponse {
  return { capture_id: captureId, preset: presetName, candidates, rejected: [] } as unknown as DevelopmentCandidatesResponse;
}

function folder(path: string): SourceFolderInfo {
  return { path, display_path: path, is_default: false, exists: true, readable: true } as SourceFolderInfo;
}

function developed(captureId: string): DevelopedResult {
  return { capture_id: captureId, cached: false, elapsed_seconds: 1.5 } as DevelopedResult;
}

function merged(captureIds: string[]): MergedResult {
  return { capture_ids: captureIds, frames: captureIds.length, cached: false, elapsed_seconds: 2, aligned: true } as MergedResult;
}

function run(state: State, ...actions: Action[]): State {
  return actions.reduce(reducer, state);
}

const withEngine: State = {
  ...initialState,
  health: HEALTH,
  engineStatus: "ready",
  analysis: analysisOf([group(1, ["A1", "A2"]), group(2, ["B1", "B2", "B3"], { kind: "bracket" })]),
};

/** capture A1 / neutral_recovery / 現世代の候補が表示中の state。 */
function stateShowingCandidates(): State {
  return run(
    withEngine,
    { type: "candidates/start", captureId: "A1", generation: 0 },
    {
      type: "candidates/ok",
      captureId: "A1",
      generation: 0,
      response: candidatesResponse("A1", "neutral_recovery", [
        candidate("bright", { exposure: 0.7, contrast: 1.2 }),
        candidate("same", {}),
      ]),
    },
  );
}

// ---------------------------------------------------------------------------
// 現像候補
// ---------------------------------------------------------------------------

describe("candidates/start", () => {
  it("前の候補を消し、busy と対象 capture / 世代を記録する", () => {
    const shown = stateShowingCandidates();
    expect(shown.developmentCandidates).not.toBeNull();
    const next = reducer(shown, { type: "candidates/start", captureId: "A2", generation: 0 });
    expect(next.developmentCandidates).toBeNull();
    expect(next.developmentCandidatesBusy).toBe(true);
    expect(next.developmentCandidatesCaptureId).toBe("A2");
    expect(next.developmentCandidatesError).toBe("");
    expect(next.developmentCandidatesGeneration).toBe(0);
  });
});

describe("candidates/ok", () => {
  const response = candidatesResponse("A1", "neutral_recovery", [candidate("x")]);

  it("start と同じ capture / 世代の応答を採用する", () => {
    const started = reducer(withEngine, { type: "candidates/start", captureId: "A1", generation: 0 });
    const next = reducer(started, { type: "candidates/ok", captureId: "A1", generation: 0, response });
    expect(next.developmentCandidates).toBe(response);
    expect(next.developmentCandidatesBusy).toBe(false);
    expect(next.noticeTone).toBe("info");
  });

  it("世代が違う応答は捨てて state を変えない", () => {
    const started = reducer(withEngine, { type: "candidates/start", captureId: "A1", generation: 0 });
    const next = reducer(started, { type: "candidates/ok", captureId: "A1", generation: 1, response });
    expect(next).toBe(started);
  });

  it("フォルダ切替後に届いた古い世代の応答は捨てる（capture_id は一致、世代だけ古い）", () => {
    const started = reducer(withEngine, { type: "candidates/start", captureId: "A1", generation: 0 });
    const switched = reducer(started, { type: "source/changed", info: folder("/new"), message: "m" });
    // 新世代で同じ capture_id のリクエストが走っている状況。capture_id 照合では弾けない。
    const restarted = reducer(switched, { type: "candidates/start", captureId: "A1", generation: 1 });
    expect(restarted.developmentCandidatesCaptureId).toBe("A1");
    const next = reducer(restarted, { type: "candidates/ok", captureId: "A1", generation: 0, response });
    expect(next).toBe(restarted);
    expect(next.developmentCandidates).toBeNull();
    expect(next.developmentCandidatesBusy).toBe(true);
  });

  it("start 時と違う capture_id の応答は捨て、後発の busy を上書きしない", () => {
    const first = reducer(withEngine, { type: "candidates/start", captureId: "A1", generation: 0 });
    const second = reducer(first, { type: "candidates/start", captureId: "A2", generation: 0 });
    const next = reducer(second, { type: "candidates/ok", captureId: "A1", generation: 0, response });
    expect(next).toBe(second);
    expect(next.developmentCandidatesBusy).toBe(true);
    expect(next.developmentCandidatesCaptureId).toBe("A2");
  });
});

describe("candidates/fail", () => {
  it("候補を消して error notice を出し busy を解除する", () => {
    const shown = stateShowingCandidates();
    const started = reducer(shown, { type: "candidates/start", captureId: "A1", generation: 0 });
    const next = reducer(started, { type: "candidates/fail", captureId: "A1", generation: 0, message: "boom" });
    expect(next.developmentCandidates).toBeNull();
    expect(next.developmentCandidatesBusy).toBe(false);
    expect(next.developmentCandidatesError).toBe("boom");
    expect(next.notice).toBe("boom");
    expect(next.noticeTone).toBe("error");
  });

  it("古い世代の失敗は無視し、いまの候補を消さない", () => {
    const shown = stateShowingCandidates();
    const next = reducer(shown, { type: "candidates/fail", captureId: "A1", generation: 99, message: "old" });
    expect(next).toBe(shown);
  });

  it("別 capture_id の失敗は無視する", () => {
    const started = reducer(withEngine, { type: "candidates/start", captureId: "A2", generation: 0 });
    const next = reducer(started, { type: "candidates/fail", captureId: "A1", generation: 0, message: "old" });
    expect(next).toBe(started);
    expect(next.developmentCandidatesBusy).toBe(true);
  });
});

describe("candidates/apply", () => {
  it("プリセットと異なる値だけを overrides に入れる", () => {
    const shown = stateShowingCandidates();
    const bright = shown.developmentCandidates!.candidates[0];
    const next = reducer(shown, { type: "candidates/apply", candidate: bright, captureId: "A1" });
    expect(next.overrides).toEqual({ exposure: 0.7, contrast: 1.2 });
    expect(next.noticeTone).toBe("info");
  });

  it("プリセットと同じ値の候補なら overrides は空", () => {
    const shown = { ...stateShowingCandidates(), overrides: { exposure: 1 } };
    const same = shown.developmentCandidates!.candidates[1];
    const next = reducer(shown, { type: "candidates/apply", candidate: same, captureId: "A1" });
    expect(next.overrides).toEqual({});
  });

  it("表示中の応答の値で適用する（引数の候補の中身は信用しない）", () => {
    const shown = stateShowingCandidates();
    const forged = candidate("bright", { exposure: -2 });
    const next = reducer(shown, { type: "candidates/apply", candidate: forged, captureId: "A1" });
    expect(next.overrides).toEqual({ exposure: 0.7, contrast: 1.2 });
  });

  it("現在の写真の候補でなければ反映せずエラー notice", () => {
    const shown = stateShowingCandidates();
    const bright = shown.developmentCandidates!.candidates[0];
    const next = reducer(shown, { type: "candidates/apply", candidate: bright, captureId: "A2" });
    expect(next.overrides).toEqual({});
    expect(next.noticeTone).toBe("error");
    expect(next.notice).toContain("反映しませんでした");
  });

  it("別プリセットの候補は反映せずエラー notice", () => {
    const shown = stateShowingCandidates();
    const bright = shown.developmentCandidates!.candidates[0];
    // reducer を経由せず、プリセットだけ食い違った状態を直接作る
    const mismatched: State = { ...shown, presetName: "shadow_rescue" };
    const next = reducer(mismatched, { type: "candidates/apply", candidate: bright, captureId: "A1" });
    expect(next.overrides).toEqual({});
    expect(next.noticeTone).toBe("error");
  });

  it("表示中の応答に無い候補 id は反映しない", () => {
    const shown = stateShowingCandidates();
    const next = reducer(shown, { type: "candidates/apply", candidate: candidate("ghost"), captureId: "A1" });
    expect(next.overrides).toEqual({});
    expect(next.noticeTone).toBe("error");
  });
});

describe("currentDevelopmentCandidates", () => {
  it("全て一致すれば候補を返す", () => {
    const shown = stateShowingCandidates();
    expect(currentDevelopmentCandidates(shown, "A1")).toHaveLength(2);
  });

  it("captureId が null / 候補なしなら空", () => {
    expect(currentDevelopmentCandidates(stateShowingCandidates(), null)).toEqual([]);
    expect(currentDevelopmentCandidates(withEngine, "A1")).toEqual([]);
  });

  it("問い合わせ capture_id が違えば空", () => {
    expect(currentDevelopmentCandidates(stateShowingCandidates(), "A2")).toEqual([]);
  });

  it("応答の capture_id が違えば空", () => {
    const shown = stateShowingCandidates();
    const state: State = {
      ...shown,
      developmentCandidates: candidatesResponse("A2", "neutral_recovery", [candidate("x")]),
    };
    expect(currentDevelopmentCandidates(state, "A1")).toEqual([]);
  });

  it("preset が違えば空", () => {
    const state: State = { ...stateShowingCandidates(), presetName: "shadow_rescue" };
    expect(currentDevelopmentCandidates(state, "A1")).toEqual([]);
  });

  it("start 時の capture_id 記録が違えば空", () => {
    const state: State = { ...stateShowingCandidates(), developmentCandidatesCaptureId: "A2" };
    expect(currentDevelopmentCandidates(state, "A1")).toEqual([]);
  });

  it("候補の世代が現在のフォルダ世代と違えば空", () => {
    const state: State = { ...stateShowingCandidates(), sourceFolderGeneration: 1 };
    expect(currentDevelopmentCandidates(state, "A1")).toEqual([]);
  });
});

describe("currentDevelopmentCandidatesResponse", () => {
  const rejected = [{ id: "foreground", reason: "白飛びが基準より3.2pt増えるため除外" }];
  const starScene = { is_star_scene: true, star_count: 12 };
  function shownWithExtras(): State {
    const shown = stateShowingCandidates();
    return {
      ...shown,
      developmentCandidates: {
        ...shown.developmentCandidates!,
        rejected,
        star_scene: starScene,
      } as unknown as DevelopmentCandidatesResponse,
    };
  }

  it("一致時は rejected / star_scene を含む応答を返す", () => {
    const res = currentDevelopmentCandidatesResponse(shownWithExtras(), "A1");
    expect(res?.rejected).toEqual(rejected);
    expect(res?.star_scene).toEqual(starScene);
    expect(res?.candidates).toHaveLength(2);
  });

  it("captureId が null なら null", () => {
    expect(currentDevelopmentCandidatesResponse(shownWithExtras(), null)).toBeNull();
  });

  it("capture_id / preset / 世代の不一致で null", () => {
    const s = shownWithExtras();
    expect(currentDevelopmentCandidatesResponse(s, "A2")).toBeNull();
    expect(currentDevelopmentCandidatesResponse({ ...s, presetName: "shadow_rescue" }, "A1")).toBeNull();
    expect(currentDevelopmentCandidatesResponse({ ...s, developmentCandidatesCaptureId: "A2" }, "A1")).toBeNull();
    expect(currentDevelopmentCandidatesResponse({ ...s, sourceFolderGeneration: 1 }, "A1")).toBeNull();
  });
});

describe("候補のクリア契機", () => {
  it("screen/operation で候補とエラーが消える（busy / capture は残る）", () => {
    const shown: State = { ...stateShowingCandidates(), developmentCandidatesError: "e" };
    const busy = { ...shown, developmentCandidatesBusy: true };
    const next = reducer(busy, { type: "screen/operation", groupId: 2 });
    expect(next.screen).toBe("operation");
    expect(next.selectedGroupId).toBe(2);
    expect(next.developmentCandidates).toBeNull();
    expect(next.developmentCandidatesError).toBe("");
    expect(next.developmentCandidatesBusy).toBe(true);
    expect(next.developmentCandidatesCaptureId).toBe("A1");
  });

  it("screen/operation は overrides を初期化し、推薦理由からプリセットを選ぶ", () => {
    const shadowGroup = group(3, ["C1"], {
      raw: { level: "strong", label: "", reason_codes: ["deep_shadows"], reasons: [] },
    } as Partial<GroupInfo>);
    const state: State = {
      ...withEngine,
      analysis: analysisOf([shadowGroup]),
      overrides: { exposure: 1 },
    };
    const next = reducer(state, { type: "screen/operation", groupId: 3 });
    expect(next.presetName).toBe("shadow_rescue");
    expect(next.overrides).toEqual({});
  });

  it("health/ok でプリセットが存在しなくなったら候補を捨てる", () => {
    const shown: State = { ...stateShowingCandidates(), presetName: "gone_preset" };
    const next = reducer(shown, { type: "health/ok", health: HEALTH });
    expect(next.presetName).toBe("neutral_recovery");
    expect(next.developmentCandidates).toBeNull();
    expect(next.developmentCandidatesError).toBe("");
  });

  it("health/ok でプリセットが変わらなければ候補は残る", () => {
    const shown = stateShowingCandidates();
    const next = reducer(shown, { type: "health/ok", health: HEALTH });
    expect(next.presetName).toBe("neutral_recovery");
    expect(next.developmentCandidates).toBe(shown.developmentCandidates);
    expect(next.engineStatus).toBe("ready");
  });

  it("preset/change で候補・capture・エラーと overrides が消える", () => {
    const shown: State = { ...stateShowingCandidates(), overrides: { exposure: 1 } };
    const next = reducer(shown, { type: "preset/change", presetName: "shadow_rescue" });
    expect(next.presetName).toBe("shadow_rescue");
    expect(next.overrides).toEqual({});
    expect(next.developmentCandidates).toBeNull();
    expect(next.developmentCandidatesCaptureId).toBeNull();
    expect(next.developmentCandidatesError).toBe("");
  });

  it("preset/change で capture_id が null に戻るため、その前の応答は採用されない", () => {
    const started = reducer(withEngine, { type: "candidates/start", captureId: "A1", generation: 0 });
    const changed = reducer(started, { type: "preset/change", presetName: "shadow_rescue" });
    const next = reducer(changed, {
      type: "candidates/ok",
      captureId: "A1",
      generation: 0,
      response: candidatesResponse("A1", "neutral_recovery", [candidate("x")]),
    });
    expect(next).toBe(changed);
  });

  it("source/changed で候補が初期化され、世代が進む", () => {
    const next = reducer(stateShowingCandidates(), { type: "source/changed", info: folder("/n"), message: "m" });
    expect(next.developmentCandidates).toBeNull();
    expect(next.developmentCandidatesBusy).toBe(false);
    expect(next.developmentCandidatesCaptureId).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// フォルダ切替の世代照合
// ---------------------------------------------------------------------------

describe("source/changed と世代", () => {
  it("世代を 1 進め、選択・結果・採用を捨てて health / preset / sourceFolder を引き継ぐ", () => {
    const before: State = {
      ...withEngine,
      presetName: "shadow_rescue",
      selectedGroupId: 1,
      screen: "operation",
      accepted: [{ kind: "photo", groupId: 1, captureId: "A1", variant: "camera_jpg" }] as State["accepted"],
      previewResults: { A1: developed("A1") },
      sourceFolderGeneration: 4,
    };
    const next = reducer(before, { type: "source/changed", info: folder("/n"), message: "切替", tone: "error" });
    expect(next.sourceFolderGeneration).toBe(5);
    expect(next.health).toBe(HEALTH);
    expect(next.engineStatus).toBe("ready");
    expect(next.presetName).toBe("shadow_rescue");
    expect(next.sourceFolder?.path).toBe("/n");
    expect(next.analysis).toBeNull();
    expect(next.accepted).toEqual([]);
    expect(next.previewResults).toEqual({});
    expect(next.selectedGroupId).toBeNull();
    expect(next.screen).toBe("suggestions");
    expect(next.notice).toBe("切替");
    expect(next.noticeTone).toBe("error");
  });

  it("tone 省略時は info", () => {
    const next = reducer(withEngine, { type: "source/changed", info: folder("/n"), message: "m" });
    expect(next.noticeTone).toBe("info");
  });

  it("連続して切り替えるたびに世代が増える", () => {
    const next = run(
      withEngine,
      { type: "source/changed", info: folder("/a"), message: "" },
      { type: "source/changed", info: folder("/b"), message: "" },
    );
    expect(next.sourceFolderGeneration).toBe(2);
  });
});

describe("develop/ok の世代照合", () => {
  it("現世代の結果は書き込み、写真の面へ切り替える", () => {
    const started = reducer(withEngine, { type: "develop/start", mode: "preview", captureId: "A1" });
    const next = reducer(started, { type: "develop/ok", mode: "preview", result: developed("A1"), generation: 0 });
    expect(next.previewResults.A1?.capture_id).toBe("A1");
    expect(next.fullResults).toEqual({});
    expect(next.developMode).toBeNull();
    expect(next.developingCapture).toBeNull();
    expect(next.viewing[1]).toEqual({ kind: "photo", captureId: "A1" });
  });

  it("rawpy の結果の通知文にはエンジン名を入れない", () => {
    const next = reducer(withEngine, { type: "develop/ok", mode: "preview", result: developed("A1"), generation: 0 });
    expect(next.notice).toBe("A1 のプレビュー現像が完了しました（1.5秒）");
    expect(next.notice).not.toContain("Core Image");
  });

  it("Core Image の結果は通知文に「Core Image・」を入れる", () => {
    const result = { ...developed("A1"), elapsed_seconds: 1.32, engine: "coreimage" } as DevelopedResult;
    const next = reducer(withEngine, { type: "develop/ok", mode: "preview", result, generation: 0 });
    expect(next.notice).toBe("A1 のプレビュー現像が完了しました（Core Image・1.3秒）");
    expect(next.noticeTone).toBe("info");
    expect(next.previewResults.A1?.engine).toBe("coreimage");
  });

  it("Core Image の保存済み結果も「Core Image・」を入れる", () => {
    const result = { ...developed("A1"), cached: true, engine: "coreimage" } as DevelopedResult;
    const next = reducer(withEngine, { type: "develop/ok", mode: "preview", result, generation: 0 });
    expect(next.notice).toBe("A1 のプレビュー現像が完了しました（Core Image・保存済み結果）");
  });

  it("rawpy へフォールバックした結果はその旨を通知する", () => {
    const next = reducer(withEngine, {
      type: "develop/ok",
      mode: "preview",
      result: developed("A1"),
      generation: 0,
      fallbackCode: "SIDECAR_NOT_FOUND",
    });
    expect(next.notice).toBe("A1: Core Image での現像に失敗したため rawpy でプレビューしました（SIDECAR_NOT_FOUND）");
    expect(next.noticeTone).toBe("info");
    expect(next.previewResults.A1).toBeDefined();
  });

  it("full は fullResults にだけ入る", () => {
    const next = reducer(withEngine, { type: "develop/ok", mode: "full", result: developed("A1"), generation: 0 });
    expect(next.fullResults.A1).toBeDefined();
    expect(next.previewResults).toEqual({});
  });

  it("古い世代の結果は捨て、state も notice も触らない", () => {
    const started = reducer(withEngine, { type: "develop/start", mode: "full", captureId: "A1" });
    const switched = reducer(started, { type: "source/changed", info: folder("/n"), message: "切替" });
    const next = reducer(switched, { type: "develop/ok", mode: "full", result: developed("A1"), generation: 0 });
    expect(next).toBe(switched);
    expect(next.fullResults).toEqual({});
    expect(next.notice).toBe("切替");
  });

  it("develop/fail は通知を出し実行中を解除する", () => {
    const started = reducer(withEngine, { type: "develop/start", mode: "full", captureId: "A1" });
    const next = reducer(started, { type: "develop/fail", message: "err" });
    expect(next.developMode).toBeNull();
    expect(next.developingCapture).toBeNull();
    expect(next.noticeTone).toBe("error");
  });
});

// ---------------------------------------------------------------------------
// ブラケット合成
// ---------------------------------------------------------------------------

describe("merge 系", () => {
  it("merge/start で実行中状態になる", () => {
    const next = reducer(withEngine, { type: "merge/start", mode: "preview", groupId: 2 });
    expect(next.mergeMode).toBe("preview");
    expect(next.mergingGroupId).toBe(2);
    expect(next.noticeTone).toBe("info");
  });

  it("merge/ok(preview) はプレビュー枠だけに入り、合成面へ切り替え、実行中を解除する", () => {
    const started = reducer(withEngine, { type: "merge/start", mode: "preview", groupId: 2 });
    const next = reducer(started, {
      type: "merge/ok",
      mode: "preview",
      groupId: 2,
      result: merged(["B1", "B2"]),
      generation: 0,
    });
    expect(next.mergePreviewResults[2]).toBeDefined();
    expect(next.mergeFullResults).toEqual({});
    expect(next.mergeMode).toBeNull();
    expect(next.mergingGroupId).toBeNull();
    expect(next.viewing[2]).toEqual({ kind: "merged" });
    expect(next.notice).toContain("2枚");
  });

  it("merge/ok(full) は本合成枠だけに入る", () => {
    const next = reducer(withEngine, {
      type: "merge/ok",
      mode: "full",
      groupId: 2,
      result: merged(["B1", "B2"]),
      generation: 0,
    });
    expect(next.mergeFullResults[2]).toBeDefined();
    expect(next.mergePreviewResults).toEqual({});
  });

  it("古い世代の merge/ok は捨てる", () => {
    const started = reducer(withEngine, { type: "merge/start", mode: "full", groupId: 2 });
    const switched = reducer(started, { type: "source/changed", info: folder("/n"), message: "切替" });
    const next = reducer(switched, {
      type: "merge/ok",
      mode: "full",
      groupId: 2,
      result: merged(["B1", "B2"]),
      generation: 0,
    });
    expect(next).toBe(switched);
    expect(next.mergeFullResults).toEqual({});
    expect(next.viewing).toEqual({});
  });

  it("merge/fail は実行中を解除してエラー通知", () => {
    const started = reducer(withEngine, { type: "merge/start", mode: "full", groupId: 2 });
    const next = reducer(started, { type: "merge/fail", message: "err" });
    expect(next.mergeMode).toBeNull();
    expect(next.mergingGroupId).toBeNull();
    expect(next.noticeTone).toBe("error");
  });

  it("merge/toggleFrame は撮影順を保って選択を作り直す（既定は全フレーム）", () => {
    const off = reducer(withEngine, { type: "merge/toggleFrame", groupId: 2, captureId: "B2" });
    expect(off.mergeSelections[2]).toEqual(["B1", "B3"]);
    const on = reducer(off, { type: "merge/toggleFrame", groupId: 2, captureId: "B2" });
    expect(on.mergeSelections[2]).toEqual(["B1", "B2", "B3"]);
  });

  it("merge/toggleFrame と resetFrames は存在しないグループなら何もしない", () => {
    expect(reducer(withEngine, { type: "merge/toggleFrame", groupId: 99, captureId: "B1" })).toBe(withEngine);
    expect(reducer(withEngine, { type: "merge/resetFrames", groupId: 99 })).toBe(withEngine);
  });

  it("merge/resetFrames は既定（先頭9枚まで）へ戻す", () => {
    const ids = Array.from({ length: 12 }, (_, i) => `X${i}`);
    const state: State = {
      ...withEngine,
      analysis: analysisOf([group(5, ids)]),
      mergeSelections: { 5: ["X0", "X1"] },
    };
    const next = reducer(state, { type: "merge/resetFrames", groupId: 5 });
    expect(next.mergeSelections[5]).toEqual(ids.slice(0, 9));
  });
});
