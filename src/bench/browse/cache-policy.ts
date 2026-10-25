/**
 * サムネ配信のキャッシュ方針の確認。配信側(vite.browse-bench.config.ts)は全ファイルに `x-bench-thumb-cache` を付けるので、
 * 計測に使うサムネ 1 枚へ HEAD を投げて読む。manifest の応答ヘッダではなく実際のサムネから取る(合成のページ内生成でも取れる)。
 * 取れなければ null を黙って出さず、理由つきで unobservable と返す。
 */
export type CachePolicyProbe =
  | { policy: string; source: "head_probe"; note: null }
  | { policy: null; source: "unobservable"; note: string };

export async function probeThumbCachePolicy(
  url: string | null,
  fetchImpl: (input: string, init: { method: string; cache: RequestCache }) => Promise<{ ok: boolean; status: number; headers: { get(name: string): string | null } }> = fetch,
): Promise<CachePolicyProbe> {
  if (url === null) return { policy: null, source: "unobservable", note: "確認するサムネ URL が無い" };
  try {
    const response = await fetchImpl(url, { method: "HEAD", cache: "no-store" });
    if (!response.ok) return { policy: null, source: "unobservable", note: `HEAD が HTTP ${response.status}` };
    const policy = response.headers.get("x-bench-thumb-cache");
    if (!policy) return { policy: null, source: "unobservable", note: "x-bench-thumb-cache ヘッダが無い(別のサーバーが配信している可能性)" };
    return { policy, source: "head_probe", note: null };
  } catch (error) {
    return { policy: null, source: "unobservable", note: `HEAD に失敗: ${error instanceof Error ? error.message : String(error)}` };
  }
}
