/**
 * 閲覧ベンチ（Issue #141 ②・#28）の入力 manifest。
 *
 * このハーネスは **実データか合成かを manifest で必ず宣言させる**（`dataset.kind`）。実メタデータ（解析の実グループ数）と
 * 合成（1000/5000 グループ等）を、報告の中で取り違えないための仕切り。**グループ数と写真数は別物**
 * （451 pairs が 451 グループとは限らない）なので、どちらも manifest と報告に別々に出す。
 *
 * ここに入れてよいのは一覧の描画に要る最小限だけ。**EXIF・GPS・絶対パスは受け付けない**
 * （未知のキーがあれば検証が落ちる）。サムネは「manifest の置き場所からの相対 URL」だけで、ローカルサーバーが
 * 同一オリジンで配る生成済みサムネ（最大辺 640px の webp/jpg）を指す。原本画像・ARW は扱わない。
 */
import type { Analysis, CaptureId, GroupInfo, GroupKind, PhotoInfo, RawLevel } from "../../lib/types";

export const BROWSE_BENCH_MANIFEST_SCHEMA = "geoframe.browse-bench.manifest/1";

export type BenchDatasetKind = "real_metadata" | "synthetic";

type DatasetCommon = {
  /** 報告に載せるラベル（英数字・`.` `_` `-` のみ）。 */
  id: string;
  /** 宣言したグループ数。実際の `groups.length` と一致しなければ検証エラー。 */
  group_count: number;
  /** 宣言した写真数。実際の `photos.length` と一致しなければ検証エラー。 */
  photo_count: number;
  /** 採点済みの解析として描くか（信頼度の表示が変わる）。省略は true。 */
  scored?: boolean;
};

export type BenchDataset =
  | (DatasetCommon & {
      kind: "real_metadata";
      real: {
        /** 例: "smb-451pairs"。出所の説明であってパスではない。 */
        source_label: string;
        /** 抽出元の解析にあった総グループ数（絞った場合は元の実値。写真数・pairs 数ではない）。 */
        source_group_count: number;
        /** どう絞ったか。 */
        selection: "all" | "first_n" | "stride";
      };
    })
  | (DatasetCommon & {
      kind: "synthetic";
      synthetic: {
        generator: string;
        seed: number;
        /** 使い回すサムネ画像の枚数。写真数以上なら 1 枚ずつ別（unique）。 */
        thumb_pool_size: number;
      };
    });

export type BenchPhoto = {
  capture_id: CaptureId;
  /** ISO8601（`YYYY-MM-DDTHH:MM...`）。カードの時刻表示にだけ使う。 */
  captured_at: string;
  /** manifest からの相対 URL（例: `thumbs/DSC02703.webp`）。絶対 URL・`/` 始まり・`..` は拒否。 */
  thumb: string;
  width?: number;
  height?: number;
  /** サムネのファイルサイズ（byte）。報告の参考値で、検証には使わない。 */
  thumb_bytes?: number;
};

export type BenchGroup = {
  id: number;
  kind: GroupKind;
  /** 例: "10:42 連写 3枚" */
  title: string;
  start_at: string;
  end_at: string;
  capture_ids: CaptureId[];
  recommended_capture_id: CaptureId;
  confidence: number;
  raw_level: RawLevel;
  raw_label: string;
  /** 推薦理由の 1 行目（カードに出る分だけ）。 */
  reason_text?: string;
};

export type BenchManifest = {
  schema: typeof BROWSE_BENCH_MANIFEST_SCHEMA;
  dataset: BenchDataset;
  photos: BenchPhoto[];
  groups: BenchGroup[];
};

export type ManifestCheck = { ok: true; manifest: BenchManifest } | { ok: false; errors: string[] };

export const MAX_GROUPS = 20_000;
export const MAX_PHOTOS = 100_000;
const MAX_ERRORS = 20;
const GROUP_KINDS: readonly GroupKind[] = ["burst", "bracket", "similar", "single"];
const RAW_LEVELS: readonly RawLevel[] = ["none", "maybe", "recommended", "strong"];
const ID_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;
const CAPTURE_ID_PATTERN = /^[A-Za-z0-9_-]{1,32}$/;
const ISO_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

/**
 * サムネ URL として許す形か。ファイルパス・外部 URL・親ディレクトリ参照を弾くため、文字種ごと絞る。
 * `/` 始まりを弾くのは、アプリ本体の `assetUrl` が `/` 始まりをローカル絶対パスと解釈して Tauri API を呼ぶため
 * （ベンチではその経路を通さない）。
 */
export function isRelativeThumbUrl(value: string): boolean {
  if (value.length === 0 || value.length > 256) return false;
  if (!/^[A-Za-z0-9._\-/]+$/.test(value)) return false;
  if (value.startsWith("/") || value.includes("//")) return false;
  return value.split("/").every((segment) => segment !== ".." && segment !== "." && segment !== "");
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNonNegativeInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

/** 許可キー以外が混ざっていたらエラー文を返す。EXIF や GPS が紛れ込む経路を塞ぐ。 */
function unknownKeys(value: Record<string, unknown>, allowed: readonly string[], where: string): string[] {
  return Object.keys(value)
    .filter((key) => !allowed.includes(key))
    .map((key) => `${where}: 未知のキー "${key}" は受け付けない`);
}

export function validateManifest(input: unknown): ManifestCheck {
  const errors: string[] = [];
  const fail = (message: string) => {
    if (errors.length < MAX_ERRORS) errors.push(message);
  };

  if (!isPlainObject(input)) return { ok: false, errors: ["manifest がオブジェクトではない"] };
  unknownKeys(input, ["schema", "dataset", "photos", "groups"], "manifest").forEach(fail);
  if (input.schema !== BROWSE_BENCH_MANIFEST_SCHEMA) fail(`schema は "${BROWSE_BENCH_MANIFEST_SCHEMA}" でなければならない`);

  const dataset = input.dataset;
  if (!isPlainObject(dataset)) {
    fail("dataset がオブジェクトではない");
  } else {
    const kind = dataset.kind;
    if (kind !== "real_metadata" && kind !== "synthetic") {
      fail('dataset.kind は "real_metadata" か "synthetic" の宣言が必須');
    } else {
      unknownKeys(
        dataset,
        ["id", "kind", "group_count", "photo_count", "scored", kind === "real_metadata" ? "real" : "synthetic"],
        "dataset",
      ).forEach(fail);
      if (typeof dataset.id !== "string" || !ID_PATTERN.test(dataset.id)) fail("dataset.id は英数字と . _ - の 1〜64 文字");
      if (!isNonNegativeInt(dataset.group_count)) fail("dataset.group_count は 0 以上の整数");
      if (!isNonNegativeInt(dataset.photo_count)) fail("dataset.photo_count は 0 以上の整数");
      if (dataset.scored !== undefined && typeof dataset.scored !== "boolean") fail("dataset.scored は boolean");
      if (kind === "real_metadata") {
        const real = dataset.real;
        if (!isPlainObject(real)) {
          fail("kind=real_metadata には dataset.real が必須");
        } else {
          unknownKeys(real, ["source_label", "source_group_count", "selection"], "dataset.real").forEach(fail);
          if (typeof real.source_label !== "string" || !ID_PATTERN.test(real.source_label)) {
            fail("dataset.real.source_label は英数字と . _ - の 1〜64 文字（パスは不可）");
          }
          if (!isNonNegativeInt(real.source_group_count)) fail("dataset.real.source_group_count は 0 以上の整数");
          if (real.selection !== "all" && real.selection !== "first_n" && real.selection !== "stride") {
            fail('dataset.real.selection は "all" | "first_n" | "stride"');
          }
        }
      } else {
        const synthetic = dataset.synthetic;
        if (!isPlainObject(synthetic)) {
          fail("kind=synthetic には dataset.synthetic が必須");
        } else {
          unknownKeys(synthetic, ["generator", "seed", "thumb_pool_size"], "dataset.synthetic").forEach(fail);
          if (typeof synthetic.generator !== "string" || synthetic.generator.length === 0 || synthetic.generator.length > 64) {
            fail("dataset.synthetic.generator は 1〜64 文字");
          }
          if (!isFiniteNumber(synthetic.seed)) fail("dataset.synthetic.seed は数値");
          if (!isNonNegativeInt(synthetic.thumb_pool_size) || synthetic.thumb_pool_size < 1) {
            fail("dataset.synthetic.thumb_pool_size は 1 以上の整数");
          }
        }
      }
    }
  }

  const photos = input.photos;
  const groups = input.groups;
  if (!Array.isArray(photos)) fail("photos が配列ではない");
  if (!Array.isArray(groups)) fail("groups が配列ではない");
  if (errors.length > 0 || !Array.isArray(photos) || !Array.isArray(groups) || !isPlainObject(dataset)) {
    return { ok: false, errors };
  }
  if (photos.length > MAX_PHOTOS) fail(`photos は ${MAX_PHOTOS} 件まで`);
  if (groups.length > MAX_GROUPS) fail(`groups は ${MAX_GROUPS} 件まで`);
  if (dataset.group_count !== groups.length) fail(`dataset.group_count(${String(dataset.group_count)}) と groups.length(${groups.length}) が違う`);
  if (dataset.photo_count !== photos.length) fail(`dataset.photo_count(${String(dataset.photo_count)}) と photos.length(${photos.length}) が違う`);
  if (dataset.kind === "real_metadata" && isPlainObject(dataset.real)) {
    const { source_group_count: sourceGroups, selection } = dataset.real;
    if (typeof sourceGroups === "number" && sourceGroups < groups.length) {
      fail(`dataset.real.source_group_count(${sourceGroups}) が groups.length(${groups.length}) より小さい`);
    }
    if (selection === "all" && sourceGroups !== groups.length) {
      fail('selection="all" なら dataset.real.source_group_count は groups.length と一致する(グループ数と写真数は別物。実値を入れる)');
    }
  }
  if (errors.length > 0) return { ok: false, errors };

  const captureIds = new Set<string>();
  photos.forEach((photo: unknown, index) => {
    const where = `photos[${index}]`;
    if (!isPlainObject(photo)) return fail(`${where} がオブジェクトではない`);
    unknownKeys(photo, ["capture_id", "captured_at", "thumb", "width", "height", "thumb_bytes"], where).forEach(fail);
    if (typeof photo.capture_id !== "string" || !CAPTURE_ID_PATTERN.test(photo.capture_id)) {
      fail(`${where}.capture_id が不正`);
    } else if (captureIds.has(photo.capture_id)) {
      fail(`${where}.capture_id "${photo.capture_id}" が重複`);
    } else {
      captureIds.add(photo.capture_id);
    }
    if (typeof photo.captured_at !== "string" || !ISO_PATTERN.test(photo.captured_at)) fail(`${where}.captured_at は ISO8601`);
    if (typeof photo.thumb !== "string" || !isRelativeThumbUrl(photo.thumb)) {
      fail(`${where}.thumb は manifest からの相対 URL のみ（絶対 URL・/ 始まり・.. ・パスは不可）`);
    }
    for (const key of ["width", "height", "thumb_bytes"] as const) {
      if (photo[key] !== undefined && !isNonNegativeInt(photo[key])) fail(`${where}.${key} は 0 以上の整数`);
    }
  });

  const groupIds = new Set<number>();
  groups.forEach((group: unknown, index) => {
    const where = `groups[${index}]`;
    if (!isPlainObject(group)) return fail(`${where} がオブジェクトではない`);
    unknownKeys(
      group,
      ["id", "kind", "title", "start_at", "end_at", "capture_ids", "recommended_capture_id", "confidence", "raw_level", "raw_label", "reason_text"],
      where,
    ).forEach(fail);
    if (!isNonNegativeInt(group.id)) fail(`${where}.id は 0 以上の整数`);
    else if (groupIds.has(group.id)) fail(`${where}.id ${group.id} が重複`);
    else groupIds.add(group.id);
    if (!GROUP_KINDS.includes(group.kind as GroupKind)) fail(`${where}.kind が不正`);
    if (typeof group.title !== "string" || group.title.length > 120) fail(`${where}.title は 120 文字以内の文字列`);
    if (typeof group.start_at !== "string" || !ISO_PATTERN.test(group.start_at)) fail(`${where}.start_at は ISO8601`);
    if (typeof group.end_at !== "string" || !ISO_PATTERN.test(group.end_at)) fail(`${where}.end_at は ISO8601`);
    if (!isFiniteNumber(group.confidence)) fail(`${where}.confidence は数値`);
    if (!RAW_LEVELS.includes(group.raw_level as RawLevel)) fail(`${where}.raw_level が不正`);
    if (typeof group.raw_label !== "string" || group.raw_label.length > 60) fail(`${where}.raw_label は 60 文字以内`);
    if (group.reason_text !== undefined && (typeof group.reason_text !== "string" || group.reason_text.length > 200)) {
      fail(`${where}.reason_text は 200 文字以内`);
    }
    const ids = group.capture_ids;
    if (!Array.isArray(ids) || ids.length === 0) {
      fail(`${where}.capture_ids は 1 件以上の配列`);
    } else {
      for (const id of ids) {
        if (typeof id !== "string" || !captureIds.has(id)) fail(`${where}.capture_ids に photos に無い ID "${String(id)}"`);
      }
      if (typeof group.recommended_capture_id !== "string" || !ids.includes(group.recommended_capture_id)) {
        fail(`${where}.recommended_capture_id が capture_ids に無い`);
      }
    }
  });

  if (errors.length > 0) return { ok: false, errors };
  return { ok: true, manifest: input as unknown as BenchManifest };
}

/** manifest の置き場所を基準に相対サムネ URL を解決する。同一オリジンでなければ null。 */
export function resolveThumbUrl(thumb: string, manifestUrl: string): string | null {
  if (!isRelativeThumbUrl(thumb)) return null;
  const base = new URL(manifestUrl);
  const resolved = new URL(thumb, base);
  return resolved.origin === base.origin ? resolved.href : null;
}

/**
 * manifest → 現行の GroupList / GroupStrip / reducer がそのまま食う `Analysis`。
 * manifest に無い採点値・EXIF は 0 / null で埋める（描画に使われないフィールド）。
 * サムネは絶対 URL（同一オリジン）にして返す。`assetUrl` は `/` 始まりだけを Tauri 変換するので、`http` 始まりは素通りする。
 */
export function manifestToAnalysis(manifest: BenchManifest, manifestUrl: string): Analysis {
  const photos: Record<CaptureId, PhotoInfo> = {};
  for (const photo of manifest.photos) {
    const thumb = resolveThumbUrl(photo.thumb, manifestUrl);
    if (thumb === null) throw new Error(`サムネ URL が同一オリジンに解決できない: ${photo.capture_id}`);
    photos[photo.capture_id] = {
      capture_id: photo.capture_id,
      captured_at: photo.captured_at,
      iso: null,
      f_number: null,
      shutter: null,
      focal_length: null,
      exposure_bias: null,
      lens_model: null,
      orientation: null,
      width: photo.width ?? 0,
      height: photo.height ?? 0,
      sharpness_raw: 0,
      sharpness_score: 0,
      highlight_clip_ratio: 0,
      shadow_clip_ratio: 0,
      mean_luma: 0,
      overall_score: 0,
      thumb,
    };
  }
  const groups: GroupInfo[] = manifest.groups.map((group) => ({
    id: group.id,
    kind: group.kind,
    title: group.title,
    start_at: group.start_at,
    end_at: group.end_at,
    capture_ids: [...group.capture_ids],
    recommended_capture_id: group.recommended_capture_id,
    confidence: group.confidence,
    recommendation_reasons: group.reason_text ? [{ code: "bench", text: group.reason_text }] : [],
    raw: { level: group.raw_level, label: group.raw_label, reason_codes: [], reasons: [] },
  }));
  return {
    analysis_version: 3,
    scored: manifest.dataset.scored ?? true,
    generated_at: "1970-01-01T00:00:00",
    analysis_revision: `bench:${manifest.dataset.id}`,
    source: { root: "(bench)", pairs: manifest.photos.length, jpg_only: 0, arw_only: 0, total_bytes: 0 },
    summary: { groups: groups.length, raw_candidates: 0, needs_review: 0 },
    photos,
    groups,
  };
}

/** 報告に載せる、データの「代表性」を損なう性質。入力から機械的に数える。 */
export type ManifestStats = {
  kind: BenchDatasetKind;
  id: string;
  /** 描画したグループ数。**写真数とは別**(1 グループに複数枚)。 */
  group_count: number;
  /** 描画した写真数(≠ グループ数)。 */
  photo_count: number;
  /** real_metadata のみ: 抽出元の解析にあった総グループ数と、どう絞ったか。synthetic は null。 */
  source: { group_count: number; selection: "all" | "first_n" | "stride" } | null;
  group_size: { min: number; max: number; mean: number };
  unique_thumb_urls: number;
  /** ユニークなサムネ URL 数 / 写真数。1 未満なら同じ画像を使い回している（キャッシュが実運用より効く）。 */
  unique_thumb_ratio: number;
  /** 同じ「撮影枚数 + サムネ列」を持つグループが重複している割合。単純複製メタデータの目安。 */
  duplicate_group_signature_ratio: number;
  thumb_bytes: { known: number; total: number; mean: number | null };
};

export function manifestStats(manifest: BenchManifest): ManifestStats {
  const thumbOf = new Map(manifest.photos.map((photo) => [photo.capture_id, photo.thumb]));
  const sizes = manifest.groups.map((group) => group.capture_ids.length);
  const signatures = new Set<string>();
  let duplicates = 0;
  for (const group of manifest.groups) {
    const signature = group.capture_ids.map((id) => thumbOf.get(id) ?? "").join("|");
    if (signatures.has(signature)) duplicates += 1;
    else signatures.add(signature);
  }
  const unique = new Set(manifest.photos.map((photo) => photo.thumb)).size;
  const bytes = manifest.photos.flatMap((photo) => (photo.thumb_bytes === undefined ? [] : [photo.thumb_bytes]));
  const total = bytes.reduce((sum, value) => sum + value, 0);
  const groupCount = manifest.groups.length;
  return {
    kind: manifest.dataset.kind,
    id: manifest.dataset.id,
    group_count: groupCount,
    photo_count: manifest.photos.length,
    source:
      manifest.dataset.kind === "real_metadata"
        ? { group_count: manifest.dataset.real.source_group_count, selection: manifest.dataset.real.selection }
        : null,
    group_size: {
      min: sizes.length ? Math.min(...sizes) : 0,
      max: sizes.length ? Math.max(...sizes) : 0,
      mean: sizes.length ? sizes.reduce((sum, value) => sum + value, 0) / sizes.length : 0,
    },
    unique_thumb_urls: unique,
    unique_thumb_ratio: manifest.photos.length ? unique / manifest.photos.length : 0,
    duplicate_group_signature_ratio: groupCount ? duplicates / groupCount : 0,
    thumb_bytes: { known: bytes.length, total, mean: bytes.length ? total / bytes.length : null },
  };
}

// ---------------------------------------------------------------------------
// 合成データ生成（決定論的）
// ---------------------------------------------------------------------------

/** mulberry32。同じ seed なら同じ列になる（テストと再現のため Math.random は使わない）。 */
export function seededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export type SyntheticOptions = {
  id: string;
  groupCount: number;
  seed: number;
  /** 使い回すサムネの枚数。写真数以上なら 1 枚ずつ別（`<prefix>/syn-<n>.jpg`）。 */
  thumbPoolSize: number;
  /** サムネの置き場（manifest からの相対ディレクトリ）。 */
  thumbPrefix: string;
};

const SYNTHETIC_SIZES = [1, 1, 2, 3, 3, 3, 4, 5, 6, 9] as const;
const SYNTHETIC_KINDS: Record<number, GroupKind> = { 1: "single", 9: "bracket" };
const KIND_LABEL: Record<GroupKind, string> = { burst: "連写", bracket: "ブラケット", similar: "類似", single: "単写" };
const RAW_FOR_KIND: Record<GroupKind, { level: RawLevel; label: string }> = {
  burst: { level: "maybe", label: "RAW現像 検討" },
  bracket: { level: "strong", label: "RAW現像 強く推奨" },
  similar: { level: "none", label: "RAW不要" },
  single: { level: "recommended", label: "RAW現像 推奨" },
};

function pad(value: number, width: number): string {
  return String(value).padStart(width, "0");
}

/**
 * 合成 manifest。写真・グループとも決定論的（seed 固定）。**実写真の分布は再現しない**ので、報告では必ず
 * `kind: "synthetic"` として扱い、実メタデータと数値を混ぜない。
 */
export function generateSyntheticManifest(options: SyntheticOptions): BenchManifest {
  // 巨大な配列を作る前に入力を検査する(100 万グループや NaN seed を生成に入れない)。
  if (!Number.isSafeInteger(options.groupCount) || options.groupCount < 1 || options.groupCount > MAX_GROUPS) {
    throw new Error(`groupCount は 1〜${MAX_GROUPS} の整数(受け取った値: ${String(options.groupCount)})`);
  }
  if (!Number.isSafeInteger(options.seed)) throw new Error(`seed は整数(受け取った値: ${String(options.seed)})`);
  if (!Number.isSafeInteger(options.thumbPoolSize) || options.thumbPoolSize < 1) {
    throw new Error(`thumbPoolSize は 1 以上の整数(受け取った値: ${String(options.thumbPoolSize)})`);
  }
  if (!ID_PATTERN.test(options.id)) throw new Error("id は英数字と . _ - の 1〜64 文字");
  if (!isRelativeThumbUrl(options.thumbPrefix)) throw new Error("thumbPrefix は相対ディレクトリ(絶対パス・.. は不可)");
  const random = seededRandom(options.seed);
  const photos: BenchPhoto[] = [];
  const groups: BenchGroup[] = [];
  const sizes: number[] = [];
  for (let index = 0; index < options.groupCount; index += 1) {
    sizes.push(SYNTHETIC_SIZES[Math.floor(random() * SYNTHETIC_SIZES.length)] ?? 1);
  }
  const photoCount = sizes.reduce((sum, value) => sum + value, 0);
  if (photoCount > MAX_PHOTOS) throw new Error(`生成される写真数 ${photoCount} が上限 ${MAX_PHOTOS} を超える(groupCount を減らす)`);
  const unique = options.thumbPoolSize >= photoCount;
  // 2026-01-01T09:00:00 から、グループ間 90 秒 + グループ内 1 枚 2 秒で並べる（時刻表示用で、意味は無い）。
  let clock = Date.UTC(2026, 0, 1, 9, 0, 0);
  let serial = 0;
  sizes.forEach((size, groupIndex) => {
    const ids: string[] = [];
    const startMs = clock;
    for (let n = 0; n < size; n += 1) {
      serial += 1;
      const captureId = `DSC${pad(serial, 5)}`;
      ids.push(captureId);
      const at = new Date(clock).toISOString().slice(0, 19);
      photos.push({
        capture_id: captureId,
        captured_at: at,
        thumb: unique
          ? `${options.thumbPrefix}/syn-${serial}.jpg`
          : `${options.thumbPrefix}/pool-${(serial - 1) % options.thumbPoolSize}.jpg`,
        width: 640,
        height: 427,
      });
      clock += 2000;
    }
    const kind: GroupKind = SYNTHETIC_KINDS[size] ?? (size === 2 ? "similar" : "burst");
    const clockLabel = new Date(startMs).toISOString().slice(11, 16);
    const raw = RAW_FOR_KIND[kind];
    groups.push({
      id: groupIndex + 1,
      kind,
      title: `${clockLabel} ${KIND_LABEL[kind]} ${size}枚`,
      start_at: new Date(startMs).toISOString().slice(0, 19),
      end_at: new Date(clock - 2000).toISOString().slice(0, 19),
      capture_ids: ids,
      recommended_capture_id: ids[Math.floor(random() * ids.length)] ?? ids[0] ?? "",
      confidence: Math.round((0.5 + random() * 0.5) * 100) / 100,
      raw_level: raw.level,
      raw_label: raw.label,
      reason_text: "合成データの推薦理由（ベンチ用の固定長テキスト）",
    });
    clock += 90_000;
  });
  const manifest: BenchManifest = {
    schema: BROWSE_BENCH_MANIFEST_SCHEMA,
    dataset: {
      id: options.id,
      kind: "synthetic",
      group_count: groups.length,
      photo_count: photos.length,
      synthetic: { generator: "generateSyntheticManifest/1", seed: options.seed, thumb_pool_size: Math.min(options.thumbPoolSize, photoCount) },
    },
    photos,
    groups,
  };
  // 生成物も実データと同じ検証を通す(宣言と実数の一致・URL 形式・ID の整合)。
  const checked = validateManifest(manifest);
  if (!checked.ok) throw new Error(`合成 manifest の検証に失敗: ${checked.errors.join("; ")}`);
  return manifest;
}

/** `?synthetic=` のクエリ(文字列)を厳密に解釈する。`1e3`・空文字・NaN・小数・負数は拒否し、上限は MAX_GROUPS。 */
export function parseSyntheticQuery(params: URLSearchParams): SyntheticOptions {
  const integer = (name: string, fallback: string | null, signed = false): number => {
    const raw = params.get(name) ?? fallback;
    if (raw === null || !(signed ? /^-?\d{1,15}$/ : /^\d{1,15}$/).test(raw)) {
      throw new Error(`?${name} は整数(受け取った値: ${raw === null ? "なし" : JSON.stringify(raw)})`);
    }
    return Number(raw);
  };
  const groupCount = integer("synthetic", null);
  if (groupCount < 1 || groupCount > MAX_GROUPS) throw new Error(`?synthetic は 1〜${MAX_GROUPS}(受け取った値: ${groupCount})`);
  const pool = integer("pool", "64");
  if (pool < 1) throw new Error("?pool は 1 以上");
  const seed = integer("seed", "1", true);
  const thumbs = params.get("thumbs") ?? "";
  if (!isRelativeThumbUrl(thumbs)) throw new Error("?thumbs=<bench-fixtures 配下の相対ディレクトリ> が必要(絶対パス・.. は不可)");
  return { id: `synthetic-${groupCount}g-pool${pool}`, groupCount, seed, thumbPoolSize: pool, thumbPrefix: thumbs };
}

// ---------------------------------------------------------------------------
// 実解析 → manifest（実メタデータの抽出。親が fixture を作るときの参考実装）
// ---------------------------------------------------------------------------

export type AnalysisSelection = { type: "all" } | { type: "first_n"; n: number } | { type: "stride"; n: number };

export type AnalysisToManifestOptions = {
  id: string;
  sourceLabel: string;
  selection: AnalysisSelection;
  /** 実解析のサムネ（絶対パス）→ manifest からの相対 URL。既定は `thumbs/<ファイル名>`。 */
  thumbUrl?: (photo: PhotoInfo) => string;
};

function defaultThumbUrl(photo: PhotoInfo): string {
  return `thumbs/${photo.thumb.split("/").pop() ?? photo.capture_id}`;
}

/** 解析結果から manifest に要る最小限だけを写す（EXIF・パス・採点値は捨てる）。 */
export function analysisToManifest(analysis: Analysis, options: AnalysisToManifestOptions): BenchManifest {
  const { selection } = options;
  const selected =
    selection.type === "all"
      ? analysis.groups
      : selection.type === "first_n"
        ? analysis.groups.slice(0, selection.n)
        : analysis.groups.filter((_, index) => index % Math.max(1, selection.n) === 0);
  const thumbUrl = options.thumbUrl ?? defaultThumbUrl;
  const used = new Set(selected.flatMap((group) => group.capture_ids));
  const photos: BenchPhoto[] = [];
  for (const [captureId, photo] of Object.entries(analysis.photos)) {
    if (!used.has(captureId)) continue;
    photos.push({
      capture_id: captureId,
      captured_at: photo.captured_at,
      thumb: thumbUrl(photo),
      width: photo.width,
      height: photo.height,
    });
  }
  const groups: BenchGroup[] = selected.map((group) => ({
    id: group.id,
    kind: group.kind,
    title: group.title,
    start_at: group.start_at,
    end_at: group.end_at,
    capture_ids: [...group.capture_ids],
    recommended_capture_id: group.recommended_capture_id,
    confidence: group.confidence,
    raw_level: group.raw.level,
    raw_label: group.raw.label,
    ...(group.recommendation_reasons[0] ? { reason_text: group.recommendation_reasons[0].text } : {}),
  }));
  return {
    schema: BROWSE_BENCH_MANIFEST_SCHEMA,
    dataset: {
      id: options.id,
      kind: "real_metadata",
      group_count: groups.length,
      photo_count: photos.length,
      scored: analysis.scored !== false,
      real: {
        source_label: options.sourceLabel,
        source_group_count: analysis.groups.length,
        selection: selection.type,
      },
    },
    photos,
    groups,
  };
}
