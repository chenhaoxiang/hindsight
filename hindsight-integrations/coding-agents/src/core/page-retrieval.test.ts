import { afterEach, describe, expect, it, vi } from "vitest";
import { HindsightClient } from "./hindsight";
import { buildKnowledgeTools } from "./knowledge-tools";
import { shapeFreshness } from "./page-freshness";
import { toPiTool } from "../harness/pi-extension";

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const makeClient = () =>
  new HindsightClient({ apiUrl: "https://fixture.invalid", bank: "synthetic-bank" });
const tool = (client: HindsightClient, name: string) =>
  buildKnowledgeTools(client, client.bank).find((t) => t.name === name)!;
afterEach(() => vi.unstubAllGlobals());

describe("agent-facing page freshness", () => {
  it("preserves stale/failure/watermark metadata with one bounded metadata batch, without trace or body duplication", async () => {
    const fetchMock = vi.fn(async (url: unknown) =>
      String(url).includes("/knowledge-base/search")
        ? json({
            results: [
              {
                id: "p1",
                name: "Synthetic decisions",
                mental_model_id: "m1",
                snippet: "old decision",
                updated_at: "2025-01-01T00:00:00Z",
              },
            ],
          })
        : json({
            items: [
              {
                id: "m1",
                is_stale: true,
                last_memory_seen_at: "2025-01-01T00:00:00Z",
                last_refreshed_at: "2025-01-01T01:00:00Z",
                last_refresh_failed_at: "2025-01-02T00:00:00Z",
                reflect_response: { trace: "synthetic-trace-not-for-context" },
                content: "synthetic-body-not-for-search",
              },
            ],
            total: 1,
          })
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await tool(makeClient(), "hindsight_search_knowledge_pages").handler({
      query: "decision",
    });
    const payload = JSON.parse(result.content[0].text);
    expect(payload.pages[0].freshness).toMatchObject({
      state: "refresh_failed",
      is_stale: true,
      last_memory_seen_at: "2025-01-01T00:00:00Z",
      source_watermark_available: true,
      basis: "server_scoped_writes; deletions_not_observed",
    });
    expect(payload.pages[0].last_updated_at).toBe("2025-01-01T00:00:00Z");
    expect(result.content[0].text).not.toContain("synthetic-trace-not-for-context");
    expect(result.content[0].text).not.toContain("synthetic-body-not-for-search");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1][0])).toContain(
      "mental-models?detail=metadata&limit=1000&offset=0"
    );
  });

  it("reads a lagging page then newer unconsolidated facts through the same bank-bound native tools", async () => {
    const fetchMock = vi.fn(async (url: unknown) => {
      const value = String(url);
      expect(value).toContain("/banks/synthetic-bank/");
      if (value.includes("/knowledge-base/search"))
        return json({
          results: [{ id: "p", name: "Older page", mental_model_id: "m", snippet: "old decision" }],
        });
      if (value.includes("/mental-models?"))
        return json({
          items: [{ id: "m", is_stale: true, last_memory_seen_at: "2025-01-01T00:00:00Z" }],
          total: 1,
        });
      expect(value).toContain("/memories/recall");
      return json({
        results: [
          {
            id: "new-fact",
            type: "world",
            text: "Synthetic newer decision",
            document_id: "new-source",
            tags: ["project:alpha"],
            metadata: { project: "alpha" },
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const c = makeClient();
    const pages = JSON.parse(
      (
        await toPiTool(tool(c, "hindsight_search_knowledge_pages")).execute("pages", {
          query: "decision",
        })
      ).content[0].text
    );
    expect(pages.pages[0].freshness.state).toBe("stale");
    const raw = JSON.parse(
      (
        await toPiTool(tool(c, "hindsight_recall")).execute("raw", {
          query: "decision",
          tags: ["project:alpha"],
        })
      ).content[0].text
    );
    expect(raw.memories[0]).toMatchObject({
      id: "new-fact",
      fact_type: "world",
      document_id: "new-source",
      metadata: { project: "alpha" },
      consolidated_at: null,
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/reflect"))).toBe(false);
  });

  it("does not infer freshness from page age when metadata is unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url) =>
        String(url).includes("/knowledge-base/search")
          ? json({
              results: [
                {
                  id: "p",
                  name: "page",
                  snippet: "historical",
                  updated_at: "2025-01-01T00:00:00Z",
                },
              ],
            })
          : json({ detail: "synthetic outage" }, 503)
      )
    );
    const payload = JSON.parse(
      (await tool(makeClient(), "hindsight_search_knowledge_pages").handler({ query: "q" }))
        .content[0].text
    );
    expect(payload.pages[0].freshness).toMatchObject({
      state: "unknown",
      last_memory_seen_at: null,
      source_watermark_available: false,
    });
  });

  it("paginates a large roster and bounds its descriptions, while internal callers keep the full roster", async () => {
    const roots = Array.from({ length: 180 }, (_, n) => ({
      id: `p${n}`,
      kind: "page",
      name: `Page ${n}`,
      mental_model_id: `m${n}`,
      description: "synthetic description ".repeat(100),
      is_stale: n === 0,
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url) =>
        String(url).includes("/tree") ? json({ roots }) : json({ items: [], total: 0 })
      )
    );
    const c = makeClient();
    expect(((await c.listPages()) as { items: unknown[] }).items).toHaveLength(180);
    const spec = tool(c, "hindsight_list_knowledge_pages");
    const first = JSON.parse((await toPiTool(spec).execute("first", {})).content[0].text);
    expect(first.total).toBe(180);
    expect(first.items.length).toBeLessThanOrEqual(25);
    expect(first.next_offset).toBe(first.items.length);
    expect(first.truncated).toBe(true);
    expect(JSON.stringify(first.items).length).toBeLessThanOrEqual(24_000);
    const next = JSON.parse(
      (await toPiTool(spec).execute("next", { offset: first.next_offset, limit: 2 })).content[0]
        .text
    );
    expect(next.items.map((p: { id: string }) => p.id)).toEqual([
      `p${first.next_offset}`,
      `p${first.next_offset + 1}`,
    ]);
  });

  it("reads the body once in bounded chunks, with scoped source freshness", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url) => {
        const u = String(url);
        if (u.includes("/knowledge-base/pages/"))
          return json({
            id: "p",
            name: "Page",
            body: "x".repeat(2500),
            markdown: "synthetic-duplicate-markdown",
          });
        if (u.includes("/tree"))
          return json({
            roots: [{ id: "p", kind: "page", name: "Page", mental_model_id: "m", is_stale: false }],
          });
        expect(u).toContain("/mental-models/m?detail=metadata");
        return json({
          id: "m",
          is_stale: false,
          last_memory_seen_at: "2025-01-03T00:00:00Z",
          last_refresh_failed_at: null,
        });
      })
    );
    const spec = tool(makeClient(), "hindsight_read_knowledge_page");
    const first = JSON.parse(
      (await spec.handler({ page_id: "p", max_chars: 1000 })).content[0].text
    );
    expect(first.body).toHaveLength(1000);
    expect(first.next_offset).toBe(1000);
    expect(first.body_total_chars).toBe(2500);
    expect(first.freshness.state).toBe("no_scoped_writes_reported");
    expect(JSON.stringify(first)).not.toContain("synthetic-duplicate-markdown");
    const second = JSON.parse(
      (await spec.handler({ page_id: "p", max_chars: 1000, offset: 2000 })).content[0].text
    );
    expect(second.body).toHaveLength(500);
    expect(second.next_offset).toBeNull();
    expect(second.body_truncated).toBe(true);
  });

  it("does not reintroduce an older tree failure after a metadata read reports it cleared", () => {
    const fresh = shapeFreshness(
      { is_stale: false, last_refresh_failed_at: null },
      { id: "p", kind: "page", name: "Page", last_refresh_failed_at: "2025-01-01T00:00:00Z" }
    );
    expect(fresh.state).toBe("no_scoped_writes_reported");
    expect(fresh.last_refresh_failed_at).toBeNull();
  });

  it("keeps a model beyond the bounded metadata page unknown instead of fetching every model body", async () => {
    const fetchMock = vi.fn(async (url) =>
      String(url).includes("/search")
        ? json({ results: [{ id: "p", name: "Page", mental_model_id: "missing", snippet: "s" }] })
        : json({
            items: Array.from({ length: 1000 }, (_, i) => ({ id: `m${i}`, is_stale: false })),
            total: 1500,
          })
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await makeClient().searchKnowledgePages("q", { includeFreshness: true });
    expect(result[0].freshness?.state).toBe("unknown");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("propagates cancellation during optional metadata reads, without reporting successful freshness", async () => {
    let ready!: () => void;
    const started = new Promise<void>((resolve) => {
      ready = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: unknown, init?: RequestInit) => {
        if (String(url).includes("/search"))
          return json({ results: [{ id: "p", name: "p", mental_model_id: "m", snippet: "s" }] });
        return new Promise<Response>((_resolve, reject) => {
          ready();
          init!.signal!.addEventListener("abort", () => reject(init!.signal!.reason), {
            once: true,
          });
        });
      })
    );
    const ctrl = new AbortController();
    const pending = toPiTool(tool(makeClient(), "hindsight_search_knowledge_pages")).execute(
      "call",
      { query: "q" },
      ctrl.signal
    );
    await started;
    ctrl.abort();
    expect((await pending).isError).toBe(true);
  });
});
