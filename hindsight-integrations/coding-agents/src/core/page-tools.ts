/**
 * Page MCP tool specs — runtime SDK-free so this stays unit-testable without a real MCP
 * host.
 *
 * `src/mcp-server.ts` is the only file with a runtime MCP SDK import; this module uses only its
 * `ToolAnnotations` type. The server wires the specs returned here into an `McpServer`. Each tool
 * wraps one `HindsightClient` page/recall method: it never throws — a thrown client error
 * is caught and turned into an `isError:true` text result so the calling LLM sees the failure
 * instead of the process crashing.
 *
 * The agent-facing surface is intentionally curated: grounding + capture only. Raw page CRUD
 * (create/update/delete) is deliberately NOT exposed — agents never author page structure; they
 * capture initiatives (`hindsight_capture_initiative`) and pages are synthesized/maintained by the
 * server. `hindsight_ingest_document` (raw-content retain) also backs the codebase survey
 * (core/survey.ts).
 */
import { z } from "zod";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ToolAnnotations } from "@modelcontextprotocol/sdk/types.js";
import type { ZodRawShape } from "zod";
import type { HindsightClient } from "./hindsight";
import { syncStatus } from "./status";
import { applyBankConfig, DEFAULT_REFLECT_TOOL_TIMEOUT_MS, loadConfig } from "./config";
import { diagFilePath } from "./diag";
import { describeError } from "./log";
import type { RetainStamp } from "./retain-stamp";
import type { PageTrigger } from "./missions";
import { diagnosticEndpoint, integrationIdentity } from "./identity";
import {
  MemoryReadUnavailableError,
  recallFacts,
  recentFacts,
  recallInput,
  recentInput,
} from "./memory-retrieval";
import { boundedPageList, boundedPageRead, boundedPageSearch } from "./page-results";

export interface ToolResult {
  // Index signature so this structurally satisfies the MCP SDK's CallToolResult (which carries
  // extra optional fields we don't set) when passed to `McpServer.tool()` in src/mcp-server.ts —
  // the only file that imports the SDK; this file stays SDK-free.
  [x: string]: unknown;
  content: { type: "text"; text: string }[];
  isError?: boolean;
}

type ToolSafetyAnnotations = Required<
  Pick<ToolAnnotations, "readOnlyHint" | "destructiveHint" | "idempotentHint" | "openWorldHint">
>;

const READ_ONLY_ANNOTATIONS: ToolSafetyAnnotations = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
  openWorldHint: false,
};

const NON_DESTRUCTIVE_WRITE_ANNOTATIONS: ToolSafetyAnnotations = {
  readOnlyHint: false,
  destructiveHint: false,
  idempotentHint: false,
  openWorldHint: false,
};

/**
 * Returned alongside every search result set.
 *
 * Phrased as an obligation triggered by the CALL, not by the agent judging that it "used" a page: a
 * model that paraphrases a snippet into its own prose does not register itself as having quoted
 * anything, and silently absorbs the memory instead of crediting it.
 */
const CREDIT_REMINDER =
  "Crediting is mandatory, not a judgement call: if anything these results contribute reaches your " +
  "reply — quoted, paraphrased, or merely confirming what you were going to say — open that part " +
  'with "> 🧠 **From Hindsight memory (<page>)** — <the specific facts you drew on>". Rewriting a ' +
  "snippet in your own words does not make it yours. If none of them bear on the turn, ignore them " +
  "silently — an unhelpful search needs no mention. These are past records: check a claim that " +
  "something was fixed or works against the code before relying on it.";

export interface ToolSpec {
  name: string;
  description: string;
  inputSchema: ZodRawShape;
  /**
   * Safety metadata published verbatim as the tool's MCP annotations (src/mcp-server.ts).
   *
   * Required, not optional: it is not cosmetic. Dcode gates every MCP tool lacking a coherent
   * read-only annotation behind an approval prompt, so in its headless (`dcode -n`) runtime an
   * unannotated tool is REJECTED outright — "This MCP action requires approval, but the current
   * headless runtime has no approval UI." Codex Auto-review likewise treats an unannotated call as
   * unverified external access. Reads get READ_ONLY_ANNOTATIONS; the two writes get
   * NON_DESTRUCTIVE_WRITE_ANNOTATIONS so clients still gate them, but for the right reason.
   */
  annotations: ToolSafetyAnnotations;
  handler: (args: any) => Promise<ToolResult>;
}

function ok(value: unknown): ToolResult {
  return { content: [{ type: "text", text: JSON.stringify(value, null, 2) }] };
}

function err(e: unknown): ToolResult {
  const message = describeError(e);
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify({
          error: message,
          ...(e instanceof MemoryReadUnavailableError ? { code: e.code } : {}),
        }),
      },
    ],
    isError: true,
  };
}

/** Wrap a handler body so a thrown client error always becomes an isError:true result, never a throw. */
function guarded(fn: (args: any) => Promise<unknown>): (args: any) => Promise<ToolResult> {
  return async (args: any) => {
    try {
      return ok(await fn(args));
    } catch (e) {
      return err(e);
    }
  };
}

/** Build the page + recall MCP tool specs, bound to one client/bank. */
export function buildPageTools(
  client: HindsightClient,
  bankId: string,
  opts: {
    repoDir?: string;
    harness?: string;
    stampFor?: () => RetainStamp;
    /** Refresh policy for a page `hindsight_capture_initiative` creates (core/missions.ts). */
    pageTrigger?: PageTrigger;
    /** How long `hindsight_reflect` waits on the server (cfg.reflectToolTimeoutMs). Must be
     *  threaded in by every caller: left unset, the client falls back to a 120s deadline that
     *  aborts high-budget synthesis on a populated bank mid-flight (#3590). */
    reflectTimeoutMs?: number;
    /** Reflect budget for `hindsight_reflect` (cfg.reflectBudget, default "high"). */
    reflectBudget?: "low" | "mid" | "high";
    /** cfg.toolGuideExtra, added after the crediting note so it lands with the results too. */
    toolGuideExtra?: string;
  } = {}
): ToolSpec[] {
  const extra = opts.toolGuideExtra?.trim();
  const crediting = extra ? `${CREDIT_REMINDER} ${extra}` : CREDIT_REMINDER;
  const listInput = {
    limit: z.number().int().min(1).max(100).default(25),
    offset: z.number().int().min(0).max(1_000_000).default(0),
  };
  const readInput = {
    page_id: z.string().min(1).max(1024),
    offset: z.number().int().min(0).max(1_000_000).default(0),
    max_chars: z.number().int().min(1000).max(48_000).default(24_000),
  };
  return [
    {
      name: "hindsight_sync_status",
      description:
        "Report whether this repo's memory bank is in sync: gitlog seed present, how much recent " +
        "history has been deepened with full diffs, conversations ingested, knowledge pages " +
        "created, and ALL async operations still active. `synced: true` covers seed ingestion, " +
        "not completion of later consolidation or page refresh. Unknown activity is not idle. " +
        "Consolidation backlog and page freshness are reported separately. Ingestion is automatic; " +
        "do not start a manual replay based on this status.",
      inputSchema: {},
      annotations: READ_ONLY_ANNOTATIONS,
      handler: async () => {
        try {
          return ok(await syncStatus(client, bankId, opts.repoDir ?? process.cwd()));
        } catch (e) {
          return err(e);
        }
      },
    },
    {
      name: "hindsight_diagnose",
      description:
        "Report safe Hindsight runtime diagnostics for this coding-agent session: resolved bank, " +
        "workspace, harness, loaded integration version versus disk version, config location, API " +
        "endpoint, and non-secret environment overrides. Optionally verify API identity with a " +
        "read-only version request (no LLM calls). Use when memory, tools or updates appear not to " +
        "work. Tokens and URL credentials are never returned.",
      inputSchema: {
        verify_connection: z
          .boolean()
          .optional()
          .describe("Read the API version, without running a model probe (default false)"),
      },
      annotations: READ_ONLY_ANNOTATIONS,
      handler: async (args: { verify_connection?: boolean }) => {
        const configPath =
          process.env.HINDSIGHT_CONFIG || join(homedir(), ".hindsight", "coding-agent.json");
        const harness = opts.harness ?? "unknown";
        // Resolve through the SAME pipeline the host used — including the `banks.<id>` section for
        // the bank this client is bound to, which may carry its own apiToken/apiUrl. A bare
        // loadConfig() would report a mismatch for every per-bank credential.
        const cfg = applyBankConfig(
          loadConfig({ harness: opts.harness }),
          bankId,
          opts.repoDir
        ).cfg;
        return ok({
          bank_id: bankId,
          harness,
          workspace: opts.repoDir ?? process.cwd(),
          integration: integrationIdentity(),
          api: {
            endpoint_in_use: diagnosticEndpoint(client.apiUrl),
            ...(args.verify_connection
              ? await client.serverIdentity(2_000)
              : { state: "not_checked", api_version: null }),
          },
          config: {
            path: configPath,
            exists: existsSync(configPath),
            api_url: diagnosticEndpoint(cfg.apiUrl),
            api_token_configured: Boolean(cfg.apiToken),
            disabled: cfg.disabled,
          },
          // What the LIVE client is signing with, which is not the same question as what the file
          // says. A long-lived host used to keep a credential the config had already replaced, and
          // this tool reported that state as perfectly healthy (#3600). Booleans only — the token
          // value is never returned.
          credential: {
            api_token_in_use: Boolean(client.apiToken),
            api_token_matches_config: client.apiToken === cfg.apiToken,
          },
          environment: {
            config_override: Boolean(process.env.HINDSIGHT_CONFIG),
            hooks_disabled: Boolean(process.env.HINDSIGHT_DISABLE_HOOKS),
            log_level: process.env.HINDSIGHT_LOG_LEVEL ?? null,
            diagnostics_file: diagFilePath(),
            channel_id_configured: Boolean(process.env.HINDSIGHT_CHANNEL_ID),
            user_id_configured: Boolean(process.env.HINDSIGHT_USER_ID),
          },
        });
      },
    },
    {
      name: "hindsight_search_knowledge_pages",
      description:
        "Search this repository's Hindsight pages for content relevant to a query — " +
        "hybrid full-text + semantic search, server-side. Call this when the user's question may " +
        "be answered by the project's accumulated knowledge (architecture, conventions, decisions, " +
        "initiatives) rather than by reading code. Returns ranked pages with a relevance snippet; " +
        // Same sentence the payload carries, from the same constant: two copies of a rule this
        // fiddly drift apart, and the description is what a host shows when the tool is listed.
        "read a full page with hindsight_read_knowledge_page. " +
        crediting,
      inputSchema: { query: z.string().trim().min(1).max(2000).describe("what to look for") },
      annotations: READ_ONLY_ANNOTATIONS,
      handler: async (args: { query: string }) => {
        try {
          // Limit comes from the client (`pageSearchLimit`), so the tool and the hook's injection
          // can never drift apart — this used to pass its own literal 3.
          const query = z.string().trim().min(1).max(2000).parse(args.query);
          const hits = await client.searchPages(query, { includeFreshness: true });
          // No `score`. The server fuses BM25 and vector search with reciprocal rank fusion, so the
          // number is ~1/(60+rank) summed over two retrievers: a perfect top hit scores about 0.03
          // and nothing ever approaches 1. Handed that, a model reads a strong match as 3% relevant
          // and discounts it. The hits arrive in rank order, which is the ranking that means
          // something here.
          // The reminder rides WITH the hits, not only in the session guide. Measured on real
          // sessions: the agent searched, got ten on-topic pages, wrote an answer built from them
          // and credited nothing — then credited correctly the moment the user asked "where did you
          // see that?". The guide had scrolled far up the context by then; the instruction that
          // lands at the same moment as the results is the one that can still be acted on.
          return ok({
            ...boundedPageSearch(hits),
            crediting,
            freshness_note:
              "Freshness is scoped-write metadata, not a current-truth guarantee; deletions are not observed. Missing metadata is unknown.",
          });
        } catch (e) {
          return err(e);
        }
      },
    },
    {
      name: "hindsight_list_knowledge_pages",
      description:
        "List this repository's Hindsight pages — curated, continuously-updated " +
        "summaries of the project's durable knowledge (architecture, components, conventions, key " +
        "decisions, and in-flight initiatives). Returns each page's id, title, and a one-line " +
        "description of what it covers, plus reported freshness. Results are bounded/paged; use " +
        "next_offset or search instead of dumping a large roster. Call at the start of substantial work, and again " +
        "periodically in long sessions, to see what the project already knows before you read code " +
        "or ask the user. The list changes as work is captured, so re-check it occasionally.",
      inputSchema: listInput,
      annotations: READ_ONLY_ANNOTATIONS,
      handler: guarded(async (input) => {
        const args = z.object(listInput).strict().parse(input);
        return boundedPageList(
          await client.listPages({ includeFreshness: true }),
          args.limit,
          args.offset
        );
      }),
    },
    {
      name: "hindsight_read_knowledge_page",
      description:
        "Read the full content of one page by its id (from " +
        "hindsight_list_knowledge_pages). Call this whenever a listed page is relevant to what " +
        "you're about to do — e.g. read Conventions before writing new code, Component map before " +
        "changing a subsystem, or an initiative's page before continuing that feature. A page may " +
        "contain [[page:<id>]] links to related pages; follow one by calling this tool again with " +
        "that id. Body reads are character-bounded; follow next_offset for the rest. Freshness " +
        "metadata may be unknown; a page is a historical synthesis, not proof of current state.",
      inputSchema: readInput,
      annotations: READ_ONLY_ANNOTATIONS,
      handler: guarded(async (input) => {
        const args = z.object(readInput).strict().parse(input);
        return boundedPageRead(
          await client.getPage(args.page_id, { includeFreshness: true }),
          args.offset,
          args.max_chars
        );
      }),
    },
    {
      name: "hindsight_recall",
      description:
        "Retrieve structured memory facts using the existing bank's recall API. Use when pages are stale, failed, generic, or miss recent evidence. Defaults to world/experience facts, including extracted facts not yet consolidated; observations are explicit opt-in. Returns fact/document IDs, provenance, tags and event timestamps with bounded results. This is not an ingestion-time filter or proof that a historical statement is still true. API ranking may use configured services; no reflect synthesis is requested. " +
        crediting,
      inputSchema: recallInput,
      annotations: READ_ONLY_ANNOTATIONS,
      handler: guarded(async (input) => ({
        ...(await recallFacts(client, input)),
        bank_id: bankId,
        crediting: crediting.replace("<page>", "<document or fact id>"),
        availability_note:
          "Raw facts are searchable after extraction, which may still be pending; consolidation/page refresh is not required.",
      })),
    },
    {
      name: "hindsight_recent_memories",
      description:
        "Read a bounded set of memory facts written/updated since an explicit ISO timestamp. Uses updated_at filtering and ordering via the existing list API, NOT recall's event-time ranking window. since is inclusive; until is exclusive and defaults to call start. Defaults to world/experience, at most one page per requested type; truncated means not exhaustive. No semantic search, no replay or refresh. " +
        crediting,
      inputSchema: recentInput,
      annotations: READ_ONLY_ANNOTATIONS,
      handler: guarded(async (input) => ({
        ...(await recentFacts(client, input)),
        bank_id: bankId,
        crediting: crediting.replace("<page>", "<document or fact id>"),
      })),
    },
    {
      name: "hindsight_reflect",
      description:
        "Deep memory reasoning: an agentic synthesis over this repository's FULL memory (git " +
        "decisions, past sessions, ingested knowledge) that answers WHY questions — the past " +
        "decision and exact rule/values that explain a behavior, bug, or convention. Slower than " +
        "hindsight_search_knowledge_pages (several seconds): reach for it when pages are too " +
        "shallow and you need the root cause or the decided literals. When the answer informs " +
        'your reply, credit it visibly with a blockquote header: "> 🧠 **From Hindsight memory** — <summary>".',
      inputSchema: { query: z.string().describe("the question to reason over memory about") },
      annotations: READ_ONLY_ANNOTATIONS,
      handler: guarded(async ({ query }: { query: string }) =>
        client.reflect(query, {
          budget: opts.reflectBudget ?? "high",
          timeoutMs: opts.reflectTimeoutMs ?? DEFAULT_REFLECT_TOOL_TIMEOUT_MS,
        })
      ),
    },
    {
      name: "hindsight_capture_initiative",
      description:
        "Record a new feature or initiative as a tracked page, so future sessions know it " +
        "exists and can build on it — and keep that page tracking the plan as it moves.\n\n" +
        "WHEN TO CALL:\n" +
        "- Starting one: right after the user approves a plan or finishes brainstorming a new feature/" +
        "capability and you are about to start implementing — BEFORE you write any code. Leave " +
        "relates_to_page_id empty.\n" +
        "- Plan changed: call it AGAIN — mid-implementation is fine and expected — whenever the goal, " +
        "scope, or rationale of an initiative you already captured materially changes (an approach is " +
        "dropped, scope grows or shrinks, the why is rewritten). Pass relates_to_page_id = that " +
        "initiative's page id so the change lands on the existing page. Never mint a second page for the " +
        "same initiative.\n" +
        "- Verified milestone or completion: update the same page with evidence. New scoped pages read " +
        "explicit captures, not unrelated shared-bank observations or ordinary session ingestion.\n\n" +
        "WHEN TO SKIP: bug fixes, small tweaks, refactors, chores, and trivial course-corrections that " +
        "leave the goal intact are not initiatives — skip them.\n\n" +
        "- title: short, specific name (e.g. 'Newsletter refinement chat'). On a recapture, reuse the " +
        "initiative's existing title.\n" +
        "- summary: 2-3 sentences on what you're building and why — the CURRENT intent, not the " +
        "originally approved plan (on a recapture, say what changed and why).\n" +
        "- relates_to_page_id: leave empty for a new initiative. Set it to an existing initiative's page " +
        "id — from hindsight_list_knowledge_pages, or the id this tool returned earlier — to record a " +
        "plan change or an enhancement to it.\n\n" +
        "Creation waits up to two minutes for the source facts. Pending/errors are not success; cancellation " +
        "does not roll back an accepted retain. Returns the real page id, not a fabricated fallback.",
      inputSchema: {
        title: z.string(),
        summary: z.string(),
        relates_to_page_id: z.string().optional(),
      },
      annotations: NON_DESTRUCTIVE_WRITE_ANNOTATIONS,
      handler: guarded(async ({ title, summary, relates_to_page_id }) =>
        client.captureInitiative({
          title,
          summary,
          relatesToPageId: relates_to_page_id,
          ...(opts.stampFor ? { stamp: opts.stampFor() } : {}),
          ...(opts.pageTrigger ? { pageTrigger: opts.pageTrigger } : {}),
        })
      ),
    },
    {
      name: "hindsight_ingest_document",
      description:
        "Save an external document or a block of durable notes/findings into this repository's " +
        "memory so it informs future recall and pages. Use for design notes, research, or " +
        "reference material you want remembered — not for the conversation you're already in " +
        "(that's captured automatically at session end). This is ALSO the correction mechanism: " +
        "when you verify that a retrieved memory is wrong or outdated, ingest a document titled " +
        "'Correction: <topic>' stating what memory claimed, what is actually true, and the " +
        "evidence — the newer fact supersedes the stale one in future retrieval.",
      inputSchema: { title: z.string(), content: z.string() },
      annotations: NON_DESTRUCTIVE_WRITE_ANNOTATIONS,
      handler: guarded(async ({ title, content }) => {
        const slug = title
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "-")
          .replace(/^-+|-+$/g, "");
        // ASCII-only slugs erased non-Latin titles (or shared the "doc" fallback),
        // overwriting unrelated documents. Hash the original title, not its content,
        // so re-ingestion still updates it; "--" cannot occur in a legacy slug.
        const docId =
          /[^\x00-\x7f]/.test(title) || !slug
            ? `${slug || "doc"}--${createHash("sha256").update(title).digest("hex")}`
            : slug;
        const stamp = opts.stampFor?.();
        const metadata = {
          ...stamp?.metadata,
          ...(opts.harness ? { harness: opts.harness } : {}),
        };
        await client.retain(
          content,
          "ingested document",
          docId,
          [
            ...new Set([
              ...(stamp?.tags ?? []),
              "source:upload",
              ...(opts.harness ? [`harness:${opts.harness}`] : []),
            ]),
          ],
          "document",
          Object.keys(metadata).length ? { metadata } : {}
        );
        return { ok: true, doc_id: docId };
      }),
    },
  ];
}
