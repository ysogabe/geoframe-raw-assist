import react from "@vitejs/plugin-react";
import { createReadStream, realpathSync, statSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

/**
 * 閲覧ベンチ(#141 ②・#28)専用の Vite 設定。プロダクトの vite.config.ts / index.html / `npm run build` とは独立。
 *
 *   dev:        `npm run bench:browse:dev`  → http://localhost:1430/browse-bench.html?...  (React は dev ビルド。遅めに出る)
 *   production: `npm run bench:browse:prod` → http://localhost:1431/browse-bench.html?...  (react-dom/profiling に差し替え。Profiler が効く本番ビルド)
 *
 * サムネ・manifest は repo 直下の `bench-fixtures/`(gitignore)を `/bench-fixtures/` で配る。
 * 画像の配信ヘッダは `BENCH_THUMB_CACHE`(`max-age`=既定 / `no-cache` / `no-store`)で切り替え、manifest 応答の
 * `x-bench-thumb-cache` として報告に残る。
 */
const FIXTURE_DIR = fileURLToPath(new URL("./bench-fixtures", import.meta.url));
const CONTENT_TYPES: Record<string, string> = {
  ".json": "application/json",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".png": "image/png",
};
const CACHE_POLICIES: Record<string, string> = {
  "max-age": "public, max-age=3600",
  "no-cache": "no-cache",
  "no-store": "no-store",
};

function benchFixtures(): Plugin {
  const policyName = process.env.BENCH_THUMB_CACHE ?? "max-age";
  const cacheControl = CACHE_POLICIES[policyName];
  if (!cacheControl) throw new Error(`BENCH_THUMB_CACHE は ${Object.keys(CACHE_POLICIES).join(" | ")} のいずれか`);

  const middleware = (request: IncomingMessage, response: ServerResponse, next: () => void) => {
    const url = request.url ?? "";
    if (!url.startsWith("/bench-fixtures/")) return next();
    let relative: string;
    try {
      relative = decodeURIComponent(url.slice("/bench-fixtures/".length).split("?")[0] ?? "");
    } catch {
      response.statusCode = 400;
      return response.end();
    }
    let file: string;
    try {
      // シンボリックリンクを解決した実体が bench-fixtures の外へ出る指定(.. や外向きリンク)は拒否する。
      file = realpathSync(path.resolve(FIXTURE_DIR, relative));
      if (!statSync(file).isFile()) throw new Error("not a file");
    } catch {
      response.statusCode = 404;
      return response.end();
    }
    if (!file.startsWith(realpathSync(FIXTURE_DIR) + path.sep)) {
      response.statusCode = 403;
      return response.end();
    }
    response.setHeader("content-type", CONTENT_TYPES[path.extname(file).toLowerCase()] ?? "application/octet-stream");
    response.setHeader("cache-control", cacheControl);
    response.setHeader("x-bench-thumb-cache", policyName);
    createReadStream(file).pipe(response);
  };

  return {
    name: "bench-fixtures",
    configureServer: (server) => void server.middlewares.use(middleware),
    configurePreviewServer: (server) => void server.middlewares.use(middleware),
  };
}

export default defineConfig(({ command }) => ({
  plugins: [react(), benchFixtures()],
  publicDir: false,
  define: { __BENCH_PROFILING__: JSON.stringify(command === "build") },
  resolve: {
    // 本番ビルドでも Profiler の onRender が呼ばれるよう profiling ビルドへ差し替える。
    alias: command === "build" ? [{ find: /^react-dom\/client$/, replacement: "react-dom/profiling" }] : [],
  },
  server: {
    port: 1430,
    strictPort: true,
    hmr: false,
    watch: { ignored: ["**/bench-fixtures/**"] },
  },
  preview: { port: 1431, strictPort: true },
  build: {
    outDir: "dist-bench",
    emptyOutDir: true,
    rollupOptions: { input: fileURLToPath(new URL("./browse-bench.html", import.meta.url)) },
  },
}));
