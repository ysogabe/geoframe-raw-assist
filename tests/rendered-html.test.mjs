import assert from "node:assert/strict";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

// app/page.tsx は "use client" だが SSR プリレンダーされるため、
// **初期 state のまま**の出力が HTML に載る。
// つまり解析データを取得する前の静的シェル（Sidebar + Topbar + AnalysisGate の
// analysisState="unknown" 分岐）だけが assert 可能で、
// GroupList / SummaryStrip / OperationPanel の内容は SSR には現れない。
test("server-renders the GeoFrame static shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();

  // レイアウト由来（常に安定）
  assert.match(html, /<title>GeoFrame RAW Assist<\/title>/i);
  assert.match(html, /<html lang="ja">/i);

  // Sidebar / Topbar は解析状態に関わらず常にマウントされる
  assert.match(html, /RAW ASSIST/);
  assert.match(html, /選ぶ時間を、撮る時間へ。/);

  // AnalysisGate の初期分析（analysisState="unknown"）
  assert.match(html, /解析状況を確認しています/);
  assert.match(html, /class="analysis-gate/);

  // 固定モックデータは全廃済み。実データはクライアント取得なので SSR には出ない。
  assert.doesNotMatch(html, /281組のペアを解析しました/);
  assert.doesNotMatch(html, /旅写真 #08/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});
