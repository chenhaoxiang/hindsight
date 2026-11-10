import { afterEach, describe, expect, it, vi } from "vitest";
import { HindsightClient } from "./hindsight";
import { buildPageTools } from "./page-tools";
import { recallFacts, recentFacts } from "./memory-retrieval";
import { toPiTool } from "../harness/pi-extension";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const client = () =>
  new HindsightClient({
    apiUrl: "https://fixture.invalid",
    bank: "bound/bank",
    recallOptions: { types: ["observation"], query: "not the tool query" },
  });
const fact = (extra = {}) => ({
  id: "fact-a",
  type: "world",
  text: "Synthetic alpha decision",
  document_id: "synthetic-doc",
  metadata: { project: "alpha", harness: "synthetic" },
  tags: ["project:alpha"],
  occurred_start: "2001-01-01T00:00:00Z",
  ...extra,
});
const window = { since: "2025-01-01T00:00:00Z", until: "2025-02-01T00:00:00Z" };
const tool = (c: HindsightClient, name: string) =>
  buildPageTools(c, c.bank).find((t) => t.name === name)!;
afterEach(() => vi.unstubAllGlobals());

describe("structured raw recall", () => {
  it("finds unconsolidated source facts with provenance without adopting injection recall overrides", async () => {
    const fetchMock = vi.fn(async () => json({ results: [fact()] }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await tool(client(), "hindsight_recall").handler({
      query: "alpha decision",
      tags: ["project:alpha"],
    });
    expect(result.isError).toBeUndefined();
    const payload = JSON.parse(result.content[0].text);
    expect(payload.bank_id).toBe("bound/bank");
    expect(payload.memories[0]).toMatchObject({
      id: "fact-a",
      fact_type: "world",
      document_id: "synthetic-doc",
      metadata: { project: "alpha" },
      tags: ["project:alpha"],
      occurred_start: "2001-01-01T00:00:00Z",
      updated_at: null,
    });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://fixture.invalid/v1/default/banks/bound%2Fbank/memories/recall");
    expect(JSON.parse(init.body as string)).toEqual({
      query: "alpha decision",
      types: ["world", "experience"],
      budget: "low",
      max_tokens: 2000,
      tags: ["project:alpha"],
      tags_match: "all_strict",
      prefer_observations: false,
      include: { entities: null },
    });
    expect(payload.crediting).toContain("document or fact id");
  });

  it.each([
    { query: "" },
    { query: "q", limit: 21 },
    { query: "q", max_tokens: 4001 },
    { query: "q", types: ["other"] },
    { query: "q", bank: "other-bank" },
    { query: "q", apiUrl: "https://other.invalid" },
    { query: "q", apiToken: "synthetic-secret" },
    { query: "q", since: window.since },
  ])("rejects unsupported/boundary-changing arguments before HTTP", async (input) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(recallFacts(client(), input)).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a tag-filter violation rather than returning cross-scope evidence", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ results: [fact({ tags: ["project:beta"] })] }))
    );
    await expect(recallFacts(client(), { query: "q", tags: ["project:alpha"] })).rejects.toThrow(
      "tag boundary"
    );
  });

  it.each(["all_strict", "any_strict", "exact"])(
    "preserves the explicit %s tag match",
    async (mode) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => json({ results: [fact()] }))
      );
      const result = await recallFacts(client(), {
        query: "q",
        tags: ["project:alpha"],
        tags_match: mode,
      });
      expect(result.tags_match).toBe(mode);
      expect(result.memories).toHaveLength(1);
    }
  );

  it("distinguishes no results, unsupported capability, service failure and malformed responses", async () => {
    const c = client(),
      spec = tool(c, "hindsight_recall");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ results: [] }))
    );
    expect(JSON.parse((await spec.handler({ query: "q" })).content[0].text).memories).toEqual([]);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "Not Found" }, 404))
    );
    const unsupported = await spec.handler({ query: "q" });
    expect(unsupported.isError).toBe(true);
    expect(JSON.parse(unsupported.content[0].text).code).toBe("memory_read_unavailable");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "synthetic outage" }, 503))
    );
    expect((await spec.handler({ query: "q" })).isError).toBe(true);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({}))
    );
    expect((await spec.handler({ query: "q" })).isError).toBe(true);
  });

  it("bounds returned text/metadata and marks omissions instead of truncating provenance identifiers", async () => {
    const data = Array.from({ length: 20 }, (_, n) =>
      fact({
        id: `fact-${n}`,
        text: "synthetic ".repeat(10_000),
        metadata: Object.fromEntries(
          Array.from({ length: 30 }, (_, k) => [`key${k}`, "value".repeat(1000)])
        ),
      })
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ results: data }))
    );
    const result = await recallFacts(client(), { query: "q", limit: 20 });
    expect(JSON.stringify(result.memories).length).toBeLessThanOrEqual(24_000);
    expect(result.memories.length).toBeGreaterThan(0);
    expect(result.truncated).toBe(true);
    expect(result.memories[0].id).toBe("fact-0");
    expect(result.memories[0].truncated).toBe(true);
  });

  it("cancels the new native recall tool without starting synthesis/fallback", async () => {
    let ready!: () => void;
    const started = new Promise<void>((resolve) => {
      ready = resolve;
    });
    const fetchMock = vi.fn(
      async (_url: unknown, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          ready();
          init!.signal!.addEventListener("abort", () => reject(init!.signal!.reason), {
            once: true,
          });
        })
    );
    vi.stubGlobal("fetch", fetchMock);
    const ctrl = new AbortController();
    const pending = toPiTool(tool(client(), "hindsight_recall")).execute(
      "fixture",
      { query: "q" },
      ctrl.signal
    );
    await started;
    ctrl.abort();
    const result = await pending;
    expect(result.isError).toBe(true);
    expect(JSON.parse(result.content[0].text).code).toBe("cancelled");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("recent writes, not event-time ranking", () => {
  it("merges bounded per-type pages by updated_at and keeps old event dates distinct", async () => {
    const fetchMock = vi.fn(async (url: unknown) => {
      const u = new URL(String(url)),
        type = u.searchParams.get("type")!;
      expect(u.searchParams.get("time_field")).toBe("updated_at");
      expect(u.searchParams.get("start_date")).toBe(window.since);
      expect(u.searchParams.get("end_date")).toBe(window.until);
      expect(u.searchParams.get("limit")).toBe("1");
      expect(u.searchParams.get("tags_match")).toBe("all_strict");
      expect(u.searchParams.getAll("tags")).toEqual(["project:alpha"]);
      return json({
        items: [
          fact({
            id: type,
            type: undefined,
            fact_type: type,
            updated_at: type === "world" ? "2025-01-20T00:00:00Z" : "2025-01-10T00:00:00Z",
            consolidated_at: null,
          }),
        ],
        total: 10,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const result = await recentFacts(client(), { ...window, limit: 1, tags: ["project:alpha"] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.memories.map((f) => f.id)).toEqual(["world"]);
    expect(result.memories[0].occurred_start).toBe("2001-01-01T00:00:00Z");
    expect(result.memories[0].updated_at).toBe("2025-01-20T00:00:00Z");
    expect(result.truncated).toBe(true);
    expect(result.time_field).toBe("updated_at");
  });

  it.each([
    { ...window, until: window.since },
    { ...window, since: "2025-01-01" },
    { ...window, time_field: "occurred_start" },
    { ...window, limit: 1000 },
    { ...window, bank_id: "other" },
  ])("rejects ambiguous/unsupported windows before HTTP", async (input) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(recentFacts(client(), input)).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([undefined, "2001-01-01T00:00:00Z", "2025-02-01T00:00:00Z", "2025-01-20T00:00:00"])(
    "does not substitute event dates for missing/invalid write time",
    async (updated) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async (url) => {
          const type = new URL(String(url)).searchParams.get("type");
          return json({
            items: [fact({ type: undefined, fact_type: type, updated_at: updated })],
            total: 1,
          });
        })
      );
      await expect(recentFacts(client(), window)).rejects.toMatchObject({
        code: "memory_read_unavailable",
      });
    }
  );

  it.each([
    ["2025-01-20T00:00:00.123100Z", "2025-01-20T00:00:00.123900Z", "2025-01-20T00:00:00.123500Z"],
    ["2025-01-20T00:00:00.123000Z", "2025-01-20T00:00:00.123900Z", "2025-01-20T00:00:00.123800Z"],
  ])(
    "honors microsecond windows and their exclusive upper bound",
    async (since, until, updated) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () =>
          json({
            items: [fact({ type: undefined, fact_type: "world", updated_at: updated })],
            total: 1,
          })
        )
      );
      const result = await recentFacts(client(), { since, until, types: ["world"] });
      expect(result.memories[0].updated_at).toBe(updated);
    }
  );

  it("selects the newer cross-type fact within one millisecond, including timezone offsets", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url) => {
        const type = new URL(String(url)).searchParams.get("type");
        return json({
          items: [
            fact({
              type: undefined,
              fact_type: type,
              id: type === "world" ? "a-older" : "z-newer",
              updated_at:
                type === "world"
                  ? "2025-01-20T01:00:00.123100+01:00"
                  : "2025-01-20T00:00:00.123900Z",
            }),
          ],
          total: 1,
        });
      })
    );
    const result = await recentFacts(client(), { ...window, limit: 1 });
    expect(result.memories.map((f) => f.id)).toEqual(["z-newer"]);
  });

  it("does not scan history for empty windows or duplicate type inputs", async () => {
    const fetchMock = vi.fn(async () => json({ items: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await recentFacts(client(), { ...window, types: ["world", "world"] });
    expect(result.memories).toEqual([]);
    expect(result.truncated).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
