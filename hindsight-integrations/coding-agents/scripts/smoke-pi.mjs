// Exercise the compiled native entrypoint with synthetic HTTP responses only.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixture = mkdtempSync(join(tmpdir(), "hindsight-compiled-smoke-"));
const originalCwd = process.cwd();
const originalFetch = globalThis.fetch;
const originalEnv = { ...process.env };
const tools = new Map();
const events = new Map();
const requests = [];
const page = {
  id: "kp-synthetic",
  kind: "page",
  name: "Synthetic page",
  mental_model_id: "mm-synthetic",
  tags: ["initiative:00000000-0000-4000-8000-000000000001"],
  body: "Synthetic source content.",
};
let unavailable = false;

try {
  process.env.HINDSIGHT_CONFIG = join(fixture, "config.json");
  process.env.HINDSIGHT_DISABLE_HOOKS = "1";
  writeFileSync(
    process.env.HINDSIGHT_CONFIG,
    JSON.stringify({
      apiUrl: "https://fixture.invalid",
      bankId: "synthetic-bank",
      autoInject: "none",
      autoSeed: false,
      autoUpdate: false,
      codebaseSurvey: false,
      retainSessions: false,
      manageBankConfig: false,
    })
  );
  process.chdir(fixture);
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    assert.equal(url.origin, "https://fixture.invalid");
    assert.ok(
      url.pathname === "/version" || url.pathname.startsWith("/v1/default/banks/synthetic-bank/")
    );
    requests.push({ path: url.pathname, method: init.method });
    let payload;
    if (url.pathname === "/version") payload = { api_version: "1.2.1-fork.1" };
    else if (unavailable) return new Response("synthetic outage", { status: 503 });
    else if (url.pathname.endsWith("/tree")) payload = { roots: [page] };
    else if (url.pathname.endsWith("/search"))
      payload = { results: [{ ...page, snippet: page.body, score: 1 }] };
    else if (url.pathname.endsWith("/kp-synthetic")) payload = page;
    else if (url.pathname.endsWith("/mental-models/mm-synthetic"))
      payload = { id: "mm-synthetic", is_stale: false };
    else if (url.pathname.endsWith("/mental-models")) payload = { items: [], total: 0 };
    else if (url.pathname.endsWith("/reflect"))
      payload = { text: "Synthetic reflection; no model was called." };
    else if (url.pathname.endsWith("/recall")) payload = { results: [] };
    else if (url.pathname.endsWith("/memories/list"))
      payload = { items: [], total: 0, limit: 10, offset: 0 };
    else if (url.pathname.endsWith("/documents")) payload = { items: [], total: 0 };
    else if (url.pathname.endsWith("/operations")) payload = { items: [], total: 0 };
    else if (url.pathname.endsWith("/stats")) payload = {};
    else if (url.pathname.endsWith("/memories") && init.method === "POST")
      payload = { operation_id: "synthetic-operation" };
    else throw new Error(`Undeclared synthetic endpoint: ${url.pathname}`);
    return new Response(JSON.stringify(payload), {
      headers: { "content-type": "application/json" },
    });
  };
  const { default: extension } = await import(pathToFileURL(join(root, "dist/pi.js")).href);
  extension({
    registerTool: (tool) => tools.set(tool.name, tool),
    on: (name, handler) => events.set(name, handler),
  });
  const args = {
    hindsight_sync_status: {},
    hindsight_diagnose: { verify_connection: true },
    hindsight_search_knowledge_pages: { query: "synthetic" },
    hindsight_list_knowledge_pages: {},
    hindsight_read_knowledge_page: { page_id: page.id },
    hindsight_reflect: { query: "synthetic" },
    hindsight_recall: { query: "synthetic" },
    hindsight_recent_memories: { since: "2026-01-01T00:00:00Z", until: "2026-01-02T00:00:00Z" },
    hindsight_capture_initiative: {
      title: "Synthetic",
      summary: "Explicit fixture update",
      relates_to_page_id: page.id,
    },
    hindsight_ingest_document: { title: "Synthetic", content: "Synthetic document only." },
  };
  assert.deepEqual([...tools.keys()].sort(), Object.keys(args).sort());
  for (const [name, input] of Object.entries(args)) {
    const result = await tools.get(name).execute(`smoke-${name}`, input);
    assert.ok(!result.isError, `${name}: ${result.content[0].text}`);
    if (name === "hindsight_diagnose") {
      const identity = JSON.parse(result.content[0].text).integration;
      assert.equal(
        identity.loaded_version,
        JSON.parse(readFileSync(join(root, "package.json"))).version
      );
    }
  }
  const beforeInvalid = requests.length;
  assert.equal(
    (await tools.get("hindsight_recall").execute("invalid", { query: "x", limit: 1000 })).isError,
    true
  );
  assert.equal(requests.length, beforeInvalid);
  const ctrl = new AbortController();
  ctrl.abort();
  const cancelled = await tools
    .get("hindsight_read_knowledge_page")
    .execute("cancelled", { page_id: page.id }, ctrl.signal);
  assert.equal(cancelled.isError, true);
  assert.equal(JSON.parse(cancelled.content[0].text).code, "cancelled");
  unavailable = true;
  assert.equal(
    (await tools.get("hindsight_recall").execute("unavailable", { query: "x" })).isError,
    true
  );
  await events.get("session_shutdown")();
  console.log(
    `Compiled Pi smoke passed: ${tools.size} tools; validation, cancellation and isError preserved; no model calls.`
  );
} finally {
  process.chdir(originalCwd);
  globalThis.fetch = originalFetch;
  for (const key of Object.keys(process.env)) if (!(key in originalEnv)) delete process.env[key];
  Object.assign(process.env, originalEnv);
  rmSync(fixture, { recursive: true, force: true });
}
