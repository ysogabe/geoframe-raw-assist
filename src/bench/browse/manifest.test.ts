import { describe, expect, it } from "vitest";
import type { Analysis, PhotoInfo } from "../../lib/types";
import {
  analysisToManifest,
  generateSyntheticManifest,
  isRelativeThumbUrl,
  manifestStats,
  MAX_GROUPS,
  manifestToAnalysis,
  parseSyntheticQuery,
  resolveThumbUrl,
  validateManifest,
} from "./manifest";

const synthetic = (overrides: Partial<Parameters<typeof generateSyntheticManifest>[0]> = {}) =>
  generateSyntheticManifest({ id: "syn", groupCount: 30, seed: 7, thumbPoolSize: 8, thumbPrefix: "syn/thumbs", ...overrides });

describe("isRelativeThumbUrl", () => {
  it("manifest からの相対パスだけを許す", () => {
    expect(isRelativeThumbUrl("thumbs/DSC00001.webp")).toBe(true);
    for (const bad of [
      "",
      "/Users/me/thumbs/a.jpg",
      "/thumbs/a.jpg",
      "https://example.com/a.jpg",
      "file:///tmp/a.jpg",
      "../a.jpg",
      "thumbs/../../a.jpg",
      "thumbs//a.jpg",
      "thumbs\\a.jpg",
      "a.jpg?x=1",
      "./a.jpg",
    ]) {
      expect(isRelativeThumbUrl(bad), bad).toBe(false);
    }
  });
});

describe("validateManifest", () => {
  it("合成 manifest を受け付ける", () => {
    expect(validateManifest(synthetic()).ok).toBe(true);
  });

  it("dataset.kind の宣言がなければ拒否する", () => {
    const manifest = JSON.parse(JSON.stringify(synthetic()));
    delete manifest.dataset.kind;
    const checked = validateManifest(manifest);
    expect(checked.ok).toBe(false);
    if (!checked.ok) expect(checked.errors.join("\n")).toContain("dataset.kind");
  });

  it("kind と詳細ブロックが食い違えば拒否する", () => {
    const manifest = JSON.parse(JSON.stringify(synthetic()));
    manifest.dataset.kind = "real_metadata";
    expect(validateManifest(manifest).ok).toBe(false);
  });

  it("EXIF・パスなど未知のキーを拒否する", () => {
    const photoWithExif = JSON.parse(JSON.stringify(synthetic()));
    photoWithExif.photos[0].gps = { lat: 1, lon: 2 };
    expect(validateManifest(photoWithExif).ok).toBe(false);
    const groupWithPath = JSON.parse(JSON.stringify(synthetic()));
    groupWithPath.groups[0].source_path = "/Users/me/x";
    expect(validateManifest(groupWithPath).ok).toBe(false);
  });

  it("宣言した件数が実数と違えば拒否する", () => {
    const manifest = JSON.parse(JSON.stringify(synthetic()));
    manifest.dataset.group_count += 1;
    expect(validateManifest(manifest).ok).toBe(false);
  });

  it("絶対 URL のサムネ・存在しない capture_id を拒否する", () => {
    const absolute = JSON.parse(JSON.stringify(synthetic()));
    absolute.photos[0].thumb = "/Users/me/Library/thumbs/a.webp";
    expect(validateManifest(absolute).ok).toBe(false);
    const dangling = JSON.parse(JSON.stringify(synthetic()));
    dangling.groups[0].capture_ids.push("DSC99999");
    expect(validateManifest(dangling).ok).toBe(false);
  });
});

describe("generateSyntheticManifest", () => {
  it("同じ seed なら同一、違う seed なら別の構成", () => {
    expect(synthetic()).toEqual(synthetic());
    expect(synthetic({ seed: 8 }).groups.map((group) => group.capture_ids.length)).not.toEqual(
      synthetic().groups.map((group) => group.capture_ids.length),
    );
  });

  it("1000/5000 グループでも検証を通り、写真数が宣言と一致する", () => {
    for (const groupCount of [1000, 5000]) {
      const manifest = synthetic({ groupCount, thumbPoolSize: 64 });
      expect(manifest.groups).toHaveLength(groupCount);
      expect(manifest.dataset.photo_count).toBe(manifest.photos.length);
      expect(manifest.dataset.kind).toBe("synthetic");
      expect(validateManifest(manifest).ok).toBe(true);
    }
  });

  it("使い回しサムネの代表性の低さを統計に出す", () => {
    const pooled = manifestStats(synthetic({ groupCount: 200, thumbPoolSize: 8 }));
    expect(pooled.unique_thumb_urls).toBe(8);
    expect(pooled.unique_thumb_ratio).toBeLessThan(0.1);
    const unique = manifestStats(synthetic({ groupCount: 50, thumbPoolSize: 1_000_000 }));
    expect(unique.unique_thumb_ratio).toBe(1);
    expect(unique.duplicate_group_signature_ratio).toBe(0);
  });
});

describe("合成入力の上限と厳密検査", () => {
  const base = { id: "syn", groupCount: 10, seed: 1, thumbPoolSize: 4, thumbPrefix: "syn/thumbs" };

  it("100 万グループ・0・小数・NaN・Infinity を配列を作る前に拒否する", () => {
    for (const groupCount of [1_000_000, MAX_GROUPS + 1, 0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() => generateSyntheticManifest({ ...base, groupCount }), String(groupCount)).toThrow("groupCount");
    }
    expect(generateSyntheticManifest({ ...base, groupCount: 5000 }).groups).toHaveLength(5000);
  });

  it("NaN/小数の seed・不正な pool・絶対パスの thumbPrefix を拒否する", () => {
    expect(() => generateSyntheticManifest({ ...base, seed: Number.NaN })).toThrow("seed");
    expect(() => generateSyntheticManifest({ ...base, seed: 1.5 })).toThrow("seed");
    expect(() => generateSyntheticManifest({ ...base, thumbPoolSize: 0 })).toThrow("thumbPoolSize");
    expect(() => generateSyntheticManifest({ ...base, thumbPoolSize: Number.NaN })).toThrow("thumbPoolSize");
    expect(() => generateSyntheticManifest({ ...base, thumbPrefix: "/Users/me/thumbs" })).toThrow("thumbPrefix");
    expect(() => generateSyntheticManifest({ ...base, thumbPrefix: "../x" })).toThrow("thumbPrefix");
    expect(() => generateSyntheticManifest({ ...base, id: "bad id/../" })).toThrow("id");
  });

  it("クエリ文字列を厳密に解釈する", () => {
    const parse = (query: string) => parseSyntheticQuery(new URLSearchParams(query));
    expect(parse("synthetic=1000&pool=64&thumbs=synthetic/thumbs")).toMatchObject({ groupCount: 1000, thumbPoolSize: 64, seed: 1 });
    expect(parse("synthetic=5000&thumbs=s/t&seed=-3&pool=8")).toMatchObject({ groupCount: 5000, seed: -3, thumbPoolSize: 8 });
    for (const bad of [
      "synthetic=1000000&thumbs=s/t",
      `synthetic=${MAX_GROUPS + 1}&thumbs=s/t`,
      "synthetic=NaN&thumbs=s/t",
      "synthetic=&thumbs=s/t",
      "synthetic=1e3&thumbs=s/t",
      "synthetic=10.5&thumbs=s/t",
      "synthetic=0&thumbs=s/t",
      "synthetic=-5&thumbs=s/t",
      "synthetic=10&seed=NaN&thumbs=s/t",
      "synthetic=10&pool=0&thumbs=s/t",
      "synthetic=10&thumbs=/abs/thumbs",
      "synthetic=10",
      "synthetic=99999999999999999999&thumbs=s/t",
    ]) {
      expect(() => parse(bad), bad).toThrow();
    }
  });
});

describe("manifestToAnalysis", () => {
  it("サムネを同一オリジンの http URL に解決する(assetUrl の Tauri 変換に入らない)", () => {
    const manifest = synthetic({ groupCount: 5 });
    const analysis = manifestToAnalysis(manifest, "http://localhost:1430/bench-fixtures/");
    const first = Object.values(analysis.photos)[0];
    expect(first?.thumb.startsWith("http://localhost:1430/bench-fixtures/syn/thumbs/")).toBe(true);
    expect(first?.thumb.startsWith("/")).toBe(false);
    expect(analysis.groups).toHaveLength(5);
    expect(analysis.groups[0]?.recommended_capture_id).toBe(manifest.groups[0]?.recommended_capture_id);
    expect(analysis.source.root).toBe("(bench)");
  });

  it("別オリジンに解決される URL は null", () => {
    expect(resolveThumbUrl("a.jpg", "http://localhost:1430/bench-fixtures/")).toBe("http://localhost:1430/bench-fixtures/a.jpg");
    expect(resolveThumbUrl("https://evil.example/a.jpg", "http://localhost:1430/bench-fixtures/")).toBeNull();
  });
});

describe("analysisToManifest", () => {
  const photo = (id: string): PhotoInfo => ({
    capture_id: id,
    captured_at: "2026-05-01T10:42:00",
    iso: 6400,
    f_number: 2.8,
    shutter: 0.01,
    focal_length: 35,
    exposure_bias: 0,
    lens_model: "SECRET LENS",
    orientation: 1,
    width: 6000,
    height: 4000,
    sharpness_raw: 1,
    sharpness_score: 2,
    highlight_clip_ratio: 0,
    shadow_clip_ratio: 0,
    mean_luma: 1,
    overall_score: 3,
    thumb: `/Users/me/Library/thumbnails/${id}.webp`,
  });
  const analysis: Analysis = {
    analysis_version: 3,
    generated_at: "2026-05-01T12:00:00",
    source: { root: "/Users/me/secret-folder", pairs: 4, jpg_only: 0, arw_only: 0, total_bytes: 1 },
    summary: { groups: 3, raw_candidates: 0, needs_review: 0 },
    photos: Object.fromEntries(["DSC00001", "DSC00002", "DSC00003", "DSC00004"].map((id) => [id, photo(id)])),
    groups: [1, 2, 3].map((id) => ({
      id,
      kind: "burst" as const,
      title: `10:4${id} 連写 1枚`,
      start_at: "2026-05-01T10:42:00",
      end_at: "2026-05-01T10:42:02",
      capture_ids: [`DSC0000${id}`],
      recommended_capture_id: `DSC0000${id}`,
      confidence: 0.9,
      recommendation_reasons: [{ code: "x", text: "理由" }],
      raw: { level: "maybe" as const, label: "RAW現像 検討", reason_codes: [], reasons: [] },
    })),
  };

  it("実メタデータの最小項目だけを写し、パス・EXIF を落とす", () => {
    const manifest = analysisToManifest(analysis, { id: "smb-g2", sourceLabel: "smb-451pairs", selection: { type: "first_n", n: 2 } });
    expect(manifest.dataset).toMatchObject({
      kind: "real_metadata",
      group_count: 2,
      photo_count: 2,
      real: { source_label: "smb-451pairs", source_group_count: 3, selection: "first_n" },
    });
    expect(validateManifest(manifest).ok).toBe(true);
    const text = JSON.stringify(manifest);
    expect(text).not.toContain("/Users/me");
    expect(text).not.toContain("SECRET LENS");
    expect(text).not.toContain("6400");
    expect(manifest.photos[0]?.thumb).toBe("thumbs/DSC00001.webp");
  });

  it("グループ数と写真数は別物として扱い、source_group_count の矛盾を拒否する", () => {
    const manifest = analysisToManifest(analysis, { id: "x", sourceLabel: "s", selection: { type: "all" } });
    expect(manifest.dataset.group_count).toBe(3);
    expect(manifest.dataset.photo_count).toBe(3);
    expect(manifestStats(manifest).source).toEqual({ group_count: 3, selection: "all" });
    const wrongAll = JSON.parse(JSON.stringify(manifest));
    wrongAll.dataset.real.source_group_count = 451;
    expect(validateManifest(wrongAll).ok).toBe(false);
    const tooSmall = JSON.parse(JSON.stringify(manifest));
    tooSmall.dataset.real.selection = "first_n";
    tooSmall.dataset.real.source_group_count = 2;
    expect(validateManifest(tooSmall).ok).toBe(false);
  });

  it("stride で間引ける", () => {
    const manifest = analysisToManifest(analysis, { id: "x", sourceLabel: "s", selection: { type: "stride", n: 2 } });
    expect(manifest.groups.map((group) => group.id)).toEqual([1, 3]);
  });
});
