import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// macOS Seatbelt blocks FSEvents, so Codex previews need polling for HMR.
const isCodexSeatbeltSandbox = process.env.CODEX_SANDBOX === "seatbelt";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 1420,
    strictPort: true,
    hmr: { port: 1421 },
    ...(isCodexSeatbeltSandbox ? { watch: { useFsEvents: false, usePolling: true } } : {}),
  },
  build: {
    outDir: "dist",
  },
  // reducer など純粋関数の単体テスト用。DOM は使わないので node 環境。
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
