import { afterEach, describe, expect, it, vi } from "vitest";
import { HindsightClient, ReflectError } from "./hindsight";
import { buildKnowledgeTools } from "./knowledge-tools";
import { toPiTool } from "../harness/pi-extension";
import { currentRequestSignal, withRequestSignal } from "./request-signal";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function waitForAbort(init?: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    if (init?.signal?.aborted) reject(init.signal.reason);
    else init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
  });
}

describe("per-tool cancellation", () => {
  it("cancels a native tool's HTTP request without cancelling another call on the same client", async () => {
    const client = new HindsightClient({ apiUrl: "https://fixture.invalid", bank: "bound-bank" });
    const held = new Map<string, { signal: AbortSignal; release: () => void }>();
    let allStarted!: () => void;
    const pendingFetches = new Promise<void>((resolve) => {
      allStarted = resolve;
    });
    const fetchMock = vi.fn(async (_url: unknown, init?: RequestInit) => {
      const id = String(_url).split("/").pop()!;
      if (id === "ok") return new Response(JSON.stringify({ id, body: "synthetic page" }));
      return new Promise<Response>((resolve, reject) => {
        const signal = init!.signal!;
        held.set(id, {
          signal,
          release: () => resolve(new Response(JSON.stringify({ id, body: "synthetic page" }))),
        });
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        if (held.size === 3) allStarted();
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const spec = buildKnowledgeTools(client, "bound-bank").find(
      (t) => t.name === "hindsight_read_knowledge_page"
    )!;
    const tool = toPiTool(spec);
    const ctrl = new AbortController();
    const pending = tool.execute("first", { page_id: "hold" }, ctrl.signal);
    const otherCtrl = new AbortController();
    const other = tool.execute("second", { page_id: "other" }, otherCtrl.signal);
    // Started outside tool scope, like ingestion, but deliberately still in flight at cancellation.
    const background = client.getPage("background");
    await pendingFetches;
    ctrl.abort();
    const cancelled = await pending;
    expect(cancelled.isError).toBe(true);
    expect(JSON.parse(cancelled.content[0].text).code).toBe("cancelled");
    expect(held.get("other")!.signal.aborted).toBe(false);
    expect(held.get("background")!.signal.aborted).toBe(false);
    expect(otherCtrl.signal.aborted).toBe(false);
    held.get("other")!.release();
    held.get("background")!.release();
    expect((await other).isError).toBeUndefined();
    expect(await background).toMatchObject({ id: "background", body: "synthetic page" });
    expect(currentRequestSignal()).toBeUndefined();
    expect((await tool.execute("third", { page_id: "ok" })).isError).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("stops a rate-limit sleep rather than sending another attempt", async () => {
    vi.useFakeTimers();
    const client = new HindsightClient({
      apiUrl: "https://fixture.invalid",
      bank: "b",
      rateLimitPatienceMs: 60_000,
    });
    const fetchMock = vi.fn(
      async () => new Response("{}", { status: 429, headers: { "retry-after": "30" } })
    );
    vi.stubGlobal("fetch", fetchMock);
    const ctrl = new AbortController();
    const pending = withRequestSignal(ctrl.signal, () =>
      client.req("GET", client.bankUrl("/stats"))
    );
    const outcome = expect(pending).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(0);
    ctrl.abort();
    await outcome;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not replay authentication after cancellation", async () => {
    const ctrl = new AbortController();
    const client = new HindsightClient({
      apiUrl: "https://fixture.invalid",
      bank: "b",
      tokenProvider: () => "synthetic-rotated-token",
    });
    const fetchMock = vi.fn(async () => {
      ctrl.abort();
      return new Response("{}", { status: 401 });
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      withRequestSignal(ctrl.signal, () => client.req("GET", client.bankUrl("/stats")))
    ).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not mislabel caller cancellation as a fallback-worthy reflect timeout", async () => {
    const ctrl = new AbortController();
    let started!: () => void;
    const ready = new Promise<void>((resolve) => {
      started = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: unknown, init?: RequestInit) => {
        started();
        return waitForAbort(init);
      })
    );
    const client = new HindsightClient({ apiUrl: "https://fixture.invalid", bank: "b" });
    const result = withRequestSignal(ctrl.signal, () =>
      client.reflect("fixture", { timeoutMs: 60_000 })
    ).catch((e) => e);
    await ready;
    ctrl.abort();
    const error = await result;
    expect(error).not.toBeInstanceOf(ReflectError);
    expect(error.name).toBe("AbortError");
  });
});
