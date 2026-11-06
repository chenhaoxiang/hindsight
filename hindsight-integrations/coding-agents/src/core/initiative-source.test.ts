import { afterEach, describe, expect, it, vi } from "vitest";
import { HindsightClient } from "./hindsight";
import { withRequestSignal } from "./request-signal";

interface Call {
  method: string;
  path: string;
  body?: {
    items?: { document_id: string; metadata?: Record<string, string>; tags?: string[] }[];
    source_query?: string;
    tags?: string[];
    trigger?: { tags_match?: string; fact_types?: string[] };
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function server(
  status = "completed",
  visible = true,
  duplicate = false,
  foreignDuplicate = false,
  ambiguousDuplicate = false
): Call[] {
  const calls: Call[] = [];
  let sourceId = "";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const path = new URL(url).pathname;
      const body = init.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ method: init.method ?? "GET", path, body });
      if (path.endsWith("/memories")) {
        sourceId = body.items[0].document_id;
        return Response.json({ operation_id: body.operation_id });
      }
      if (path.includes("/operations/")) return Response.json({ status });
      if (path.endsWith("/memories/list"))
        return Response.json({
          total: visible ? 1 : 0,
          items: visible ? [{ document_id: sourceId }] : [],
        });
      if (path.endsWith("/tree"))
        return Response.json({
          roots:
            duplicate && sourceId
              ? [
                  {
                    id: "folder",
                    kind: "folder",
                    name: "Initiatives",
                    children: [
                      {
                        id: "kp-existing",
                        kind: "page",
                        name: "Atlas cache repair",
                        tags: [
                          "knowledge:feature-work",
                          `initiative:${foreignDuplicate ? "aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee" : sourceId.slice("initiative-source-".length)}`,
                          ...(ambiguousDuplicate
                            ? ["initiative:aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee"]
                            : []),
                        ],
                      },
                    ],
                  },
                ]
              : [],
        });
      if (path.endsWith("/folders")) return Response.json({ id: "folder" });
      if (path.endsWith("/pages"))
        return duplicate
          ? Response.json({ detail: "Already exists" }, { status: 409 })
          : Response.json({ page_id: "kp-real" });
      throw new Error(`Undeclared fixture request: ${path}`);
    })
  );
  return calls;
}

const capture = (
  client = new HindsightClient({ apiUrl: "http://fixture.invalid", bank: "shared" })
) =>
  client.captureInitiative({
    title: "Atlas cache repair",
    summary: "Repair Atlas's cache, not the unrelated Boreal rollout.",
  });

describe("initiative source readiness", () => {
  it("commits and verifies the identifying source before creating an auto-synthesizing page", async () => {
    const calls = server();
    await expect(capture()).resolves.toEqual({ page_id: "kp-real" });
    const page = calls.findIndex((c) => c.path.endsWith("/pages"));
    const source = calls.findIndex((c) => c.path.endsWith("/memories"));
    const visible = calls.findIndex((c) => c.path.endsWith("/memories/list"));
    expect(source).toBeLessThan(visible);
    expect(visible).toBeLessThan(page);
    const query = calls[page].body?.source_query ?? "";
    expect(query).toContain("Atlas cache repair");
    expect(query).toContain("Repair Atlas's cache");
    expect(query).toContain(calls[source].body?.items?.[0].document_id);
    expect(query).not.toContain("group by project where that helps");
    expect(calls[page].body?.tags).toEqual([
      "knowledge:feature-work",
      expect.stringMatching(/^initiative:/),
    ]);
    expect(calls[page].body?.trigger?.tags_match).toBe("all_strict");
    expect(calls[page].body?.trigger?.fact_types).toEqual(["world", "experience"]);
    const markers = calls.filter((c) => c.path.endsWith("/memories"));
    expect(markers.at(-1)?.body?.items?.[0].metadata?.relatedPageId).toBe("kp-real");
  });

  it("recovers the real same-source page after a duplicate create acknowledgement", async () => {
    const calls = server("completed", true, true);
    await expect(capture()).resolves.toEqual({ page_id: "kp-existing" });
    expect(calls.filter((c) => c.path.endsWith("/pages"))).toHaveLength(1);
  });

  it("does not adopt another plan's same-name duplicate page", async () => {
    const calls = server("completed", true, true, true);
    await expect(capture()).rejects.toThrow("no page id invented");
    expect(calls.filter((c) => c.path.endsWith("/memories"))).toHaveLength(1);
  });

  it("does not adopt a same-source duplicate with ambiguous initiative tags", async () => {
    const calls = server("completed", true, true, false, true);
    await expect(capture()).rejects.toThrow("no page id invented");
    expect(calls.filter((c) => c.path.endsWith("/memories"))).toHaveLength(1);
  });

  it("reuses the same initial source identity on an identical retry", async () => {
    const calls = server();
    await capture();
    await capture();
    const sourceIds = calls
      .filter((c) => c.path.endsWith("/memories") && !c.body?.items?.[0].metadata?.relatedPageId)
      .map((c) => c.body?.items?.[0].document_id);
    expect(sourceIds).toHaveLength(2);
    expect(sourceIds[0]).toBe(sourceIds[1]);
  });

  it.each(["failed", "cancelled", "not_found", "unexpected"])(
    "does not create a page after source status %s",
    async (status) => {
      const calls = server(status);
      await expect(capture()).rejects.toThrow();
      expect(calls.some((c) => c.path.endsWith("/pages"))).toBe(false);
    }
  );

  it("does not equate completed extraction with visible source facts", async () => {
    const calls = server("completed", false);
    await expect(capture()).rejects.toThrow();
    expect(calls.some((c) => c.path.endsWith("/pages"))).toBe(false);
  });

  it("copies an existing initiative's source tag onto its update marker", async () => {
    const sourceTag = "initiative:11111111-2222-5333-8444-555555555555";
    const requests: Call[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        const path = new URL(url).pathname;
        const body = init.body ? JSON.parse(String(init.body)) : undefined;
        requests.push({ path, method: init.method ?? "GET", body });
        if (path.endsWith("/pages/kp-existing"))
          return Response.json({ tags: ["knowledge:feature-work", sourceTag] });
        if (path.endsWith("/memories")) return Response.json({ operation_id: "update-op" });
        throw new Error(`Undeclared fixture request: ${path}`);
      })
    );
    const client = new HindsightClient({ apiUrl: "http://fixture.invalid", bank: "shared" });
    await client.captureInitiative({
      title: "Reworded title",
      summary: "Verified completion",
      relatesToPageId: "kp-existing",
    });
    expect(requests.at(-1)?.body?.items?.[0].tags).toContain(sourceTag);
    expect(requests.some((r) => r.path.endsWith("/pages"))).toBe(false);
  });

  it("keeps a completed but unacknowledged page id as an error, not a guessed slug", async () => {
    const calls = server();
    const routed = globalThis.fetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        if (new URL(url).pathname.endsWith("/pages")) return Response.json({});
        return routed(url, init);
      })
    );
    await expect(capture()).rejects.toThrow("no page id invented");
    expect(calls.filter((c) => c.path.endsWith("/memories"))).toHaveLength(1);
  });

  it("bounds the source wait instead of treating pending as success", async () => {
    const calls = server("pending");
    vi.spyOn(Date, "now").mockReturnValueOnce(0).mockReturnValue(120_001);
    await expect(capture()).rejects.toThrow("still pending");
    expect(calls.some((c) => c.path.endsWith("/pages"))).toBe(false);
  });

  it.each(["operations", "facts"])(
    "does not spend global rate-limit patience during the bounded %s read",
    async (stage) => {
      const calls = server();
      const routed = globalThis.fetch;
      vi.spyOn(Math, "random").mockReturnValue(0.5);
      vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string, init: RequestInit) => {
          const path = new URL(url).pathname;
          if (
            stage === "operations" ? path.includes("/operations/") : path.endsWith("/memories/list")
          )
            return Response.json({}, { status: 429, headers: { "Retry-After": "60" } });
          return routed(url, init);
        })
      );
      const ctrl = new AbortController();
      // Bound a broken test too: the old req() would sleep 75 seconds on this 429.
      const guard = setTimeout(() => ctrl.abort(), 1_000);
      try {
        const client = new HindsightClient({
          apiUrl: "http://fixture.invalid",
          bank: "shared",
          rateLimitPatienceMs: 300_000,
        });
        const result = withRequestSignal(ctrl.signal, () => capture(client));
        await expect(result).rejects.toThrow(
          stage === "operations" ? "source did not complete" : "no visible extracted facts"
        );
        expect(ctrl.signal.aborted).toBe(false);
        expect(calls.some((c) => c.path.endsWith("/pages"))).toBe(false);
        expect(calls.some((c) => c.method === "DELETE")).toBe(false);
      } finally {
        clearTimeout(guard);
        ctrl.abort();
      }
    }
  );

  it("rejects facts whose response body finishes after the absolute source deadline", async () => {
    const calls = server();
    const routed = globalThis.fetch;
    let now = 0;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        const parsed = new URL(url);
        if (parsed.pathname.endsWith("/memories/list")) {
          const response = Response.json({});
          response.json = async () => {
            now = 120_001;
            return { items: [{ document_id: parsed.searchParams.get("document_id") }] };
          };
          return response;
        }
        return routed(url, init);
      })
    );
    await expect(capture()).rejects.toThrow("still pending");
    expect(calls.some((c) => c.path.endsWith("/pages"))).toBe(false);
  });

  it("refuses an update whose page carries multiple initiative source tags", async () => {
    const fetch = vi.fn(async () =>
      Response.json({
        tags: [
          "initiative:11111111-2222-5333-8444-555555555555",
          "initiative:aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee",
        ],
      })
    );
    vi.stubGlobal("fetch", fetch);
    const client = new HindsightClient({ apiUrl: "http://fixture.invalid", bank: "shared" });
    await expect(
      client.captureInitiative({ title: "Atlas", summary: "Done", relatesToPageId: "kp-ambiguous" })
    ).rejects.toThrow("ambiguous source scope");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("cancels a pending wait without creating a page or cancelling the accepted retain", async () => {
    const calls = server("pending");
    const ctrl = new AbortController();
    const result = withRequestSignal(ctrl.signal, () => capture());
    await vi.waitFor(() => expect(calls.some((c) => c.path.includes("/operations/"))).toBe(true));
    ctrl.abort();
    await expect(result).rejects.toThrow();
    expect(calls.some((c) => c.path.endsWith("/pages"))).toBe(false);
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  });
});
