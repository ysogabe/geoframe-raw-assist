import { describe, expect, it } from "vitest";
import { probeThumbCachePolicy } from "./cache-policy";

const response = (status: number, header: string | null) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (name: string) => (name === "x-bench-thumb-cache" ? header : null) },
});

describe("probeThumbCachePolicy", () => {
  it("HEAD の x-bench-thumb-cache を読む", async () => {
    let seen: { url: string; method: string } | null = null;
    const result = await probeThumbCachePolicy("http://localhost:1431/bench-fixtures/a/thumbs/x.jpg", async (url, init) => {
      seen = { url, method: init.method };
      return response(200, "no-store");
    });
    expect(result).toEqual({ policy: "no-store", source: "head_probe", note: null });
    expect(seen).toEqual({ url: "http://localhost:1431/bench-fixtures/a/thumbs/x.jpg", method: "HEAD" });
  });

  it("取れないときは null を黙って返さず、理由つきの unobservable にする", async () => {
    expect(await probeThumbCachePolicy(null)).toMatchObject({ policy: null, source: "unobservable" });
    expect(await probeThumbCachePolicy("http://x/a.jpg", async () => response(404, null))).toMatchObject({
      source: "unobservable",
      note: expect.stringContaining("404"),
    });
    expect(await probeThumbCachePolicy("http://x/a.jpg", async () => response(200, null))).toMatchObject({
      source: "unobservable",
      note: expect.stringContaining("ヘッダが無い"),
    });
    expect(
      await probeThumbCachePolicy("http://x/a.jpg", async () => {
        throw new Error("network down");
      }),
    ).toMatchObject({ source: "unobservable", note: expect.stringContaining("network down") });
  });
});
