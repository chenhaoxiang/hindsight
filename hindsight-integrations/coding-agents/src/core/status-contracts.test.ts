import { afterEach, describe, expect, it, vi } from "vitest";
import { HindsightClient } from "./hindsight";

const client = () => new HindsightClient({ apiUrl: "https://fixture.invalid", bank: "bound/bank" });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => vi.unstubAllGlobals());

describe("authoritative operation totals", () => {
  it("counts older pending work beyond a newest page of completed operations with two bounded reads", async () => {
    const fetchMock = vi.fn(async (url: unknown) => {
      const u = new URL(String(url));
      expect(u.pathname).toBe("/v1/default/banks/bound%2Fbank/operations");
      expect(u.searchParams.get("limit")).toBe("1");
      expect(u.searchParams.get("offset")).toBe("0");
      const status = u.searchParams.get("status");
      if (status === "pending") return json({ total: 237, operations: [{ status: "pending" }] });
      if (status === "processing")
        return json({ total: 3, operations: [{ status: "processing" }] });
      return json({
        total: 1000,
        operations: Array.from({ length: 20 }, () => ({ status: "completed" })),
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    expect(await client().activeOperations()).toBe(240);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("accepts an explicit empty filtered result as zero", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ total: 0, operations: [] }))
    );
    expect(await client().activeOperations()).toBe(0);
  });

  it.each([
    {},
    null,
    { total: "0", operations: [] },
    { total: -1, operations: [] },
    { total: 1.5, operations: [] },
    { total: 5 },
    { total: 1, operations: [{ status: "completed" }] },
    { total: 0, operations: [{ status: "pending" }] },
  ])("rejects unknown or invalid status instead of inventing zero", async (body) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(body))
    );
    await expect(client().activeOperations()).rejects.toThrow();
  });

  it.each([404, 500])("does not turn HTTP %s into zero", async (status) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "synthetic unavailable" }, status))
    );
    await expect(client().activeOperations()).rejects.toThrow();
  });

  it("keeps consolidation counts distinct and marks server cache age unknown", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          pending_consolidation: 42,
          failed_consolidation: 2,
          last_consolidated_at: "2025-01-01T00:00:00Z",
          last_memory_write_at: "2025-01-02T00:00:00Z",
        })
      )
    );
    expect(await client().consolidationStatus()).toEqual({
      pending: 42,
      failed: 2,
      last_consolidated_at: "2025-01-01T00:00:00Z",
      last_memory_write_at: "2025-01-02T00:00:00Z",
      freshness: "server_reported_cache_age_unknown",
    });
  });

  it("keeps missing backlog fields unknown on older APIs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ pending_consolidation: "0", failed_consolidation: -1 }))
    );
    const result = await client().consolidationStatus();
    expect(result.pending).toBeNull();
    expect(result.failed).toBeNull();
  });
});

describe("API identity", () => {
  it.each(["0.9.0rc1", "1.2.0-fork.1+build.2", "0.9", "1.2.0.post1.dev2+fixture"])(
    "preserves append capability and diagnostic identity for %s",
    async (version) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async (url) =>
          String(url).endsWith("/config")
            ? json({ config: { store_document_text: true } })
            : json({ api_version: version })
        )
      );
      const c = client();
      expect(await c.supportsIdempotentRetain()).toBe(true);
      expect(await c.supportsAppendRetain()).toBe(true);
      expect(await c.serverIdentity()).toEqual({ state: "available", api_version: version });
    }
  );
  it("falls back to the legacy version alias and never guesses from the integration version", async () => {
    const fetchMock = vi.fn(async (url) =>
      String(url).endsWith("/v1/version")
        ? json({ detail: "Not Found" }, 404)
        : json({ api_version: "1.2.0+fixture.1" })
    );
    vi.stubGlobal("fetch", fetchMock);
    expect(await client().serverIdentity()).toEqual({
      state: "available",
      api_version: "1.2.0+fixture.1",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([{}, { api_version: "not a version" }, { api_version: 123 }])(
    "reports unparseable versions as unknown",
    async (body) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => json(body))
      );
      expect(await client().serverIdentity()).toEqual({ state: "unknown", api_version: null });
    }
  );
});
