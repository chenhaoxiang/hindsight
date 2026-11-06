/**
 * Bank sync status — the observable contract that replaced the manual backfill CLI.
 *
 * Ingestion is automatic and background (core/seed.ts spawns the deepen engine at session start),
 * so "is my memory ready?" needs an inspectable answer instead of a command exit code. `syncStatus`
 * compares the LIVE bank against the repo: gitlog seed present, how much of the recent history has
 * been deepened (per-commit diffs), chats ingested, knowledge pages present, extractions still
 * running. Agents get it as the `hindsight_sync_status` tool; harnesses (e.g. the benchmark) poll
 * the same via `dist/status.js`.
 *
 * `synced` is the completion marker for the REQUIRED memory: gitlog seeded, pages created, and no
 * async operation still active. This is a seed-ingestion check, NOT a claim that every later write
 * has been consolidated or every page refreshed. The per-commit diff
 * trickle deliberately does NOT gate `synced` — it deepens across sessions and is a bonus, not a
 * prerequisite.
 */
import { execFileSync } from "node:child_process";
import { commitsSince, repoNameOf } from "./git";
import { parsePageList } from "./knowledge-injection";
import { SURVEY_DOC_IDS } from "./survey";
import type { ConsolidationStatus } from "./hindsight";

/** Minimal client shape (HindsightClient satisfies it structurally). */
export interface StatusClient {
  listDocumentIds(tag: string, tagsMatch?: "all" | "all_strict"): Promise<Set<string>>;
  listPages(opts?: { includeFreshness?: boolean }): Promise<unknown>;
  knowledgePagesSupported?: boolean;
  activeOperations(): Promise<number>;
  consolidationStatus?(): Promise<ConsolidationStatus>;
}

/** How much recent history the deepen engine targets with per-commit diffs. */
export const DEEPEN_DIFF_TARGET = 300;

export interface SyncStatus {
  bank: string;
  gitlogPresent: boolean; // the aggregated commit-message seed document exists
  gitDiffDocs: number; // commits ingested individually with full diffs (git:<sha>)
  gitDiffTarget: number | null; // min(DEEPEN_DIFF_TARGET, repo commits); null when repo unknown
  chatDocs: number; // past/live conversations in the bank
  pagesCount: number;
  surveyBaseline: string | null; // HEAD sha at the last codebase survey START (null = never started)
  surveyDocs: number; // findings documents present (of 4) — completion signal, not just started
  surveyCommitsBehind: number | null; // commits since that baseline (null = no baseline / no repo)
  activeOps: number | null; // ALL pending/processing ops; null if the server can't report
  seedReady: boolean;
  activity: "active" | "idle" | "unknown";
  consolidation: ConsolidationStatus | null;
  pageFreshness: "unknown" | "stale" | "refresh_failed" | "no_scoped_writes_reported";
  syncScope: "seed_ingestion";
  synced: boolean;
}

function commitCount(repoDir: string): number | null {
  try {
    const out = execFileSync("git", ["-C", repoDir, "rev-list", "--count", "HEAD"], {
      encoding: "utf8",
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"], // failed probes must not write into the host TUI
    });
    return Number(out.trim()) || 0;
  } catch {
    return null; // not a git repo / no commits
  }
}

export async function syncStatus(
  client: StatusClient,
  bank: string,
  repoDir?: string
): Promise<SyncStatus> {
  const gitIds = await client.listDocumentIds("source:git", "all_strict");
  const chatIds = await client
    .listDocumentIds("source:chat", "all_strict")
    .catch(() => new Set<string>());
  const pagePayload = await client.listPages({ includeFreshness: true }).catch(() => null);
  const pages = parsePageList(pagePayload);
  const pageItems = (pagePayload as { items?: { freshness?: { state?: string } }[] } | null)?.items;
  const states = Array.isArray(pageItems) ? pageItems.map((p) => p?.freshness?.state) : undefined;
  const pageFreshness = states?.includes("refresh_failed")
    ? "refresh_failed"
    : states?.includes("stale")
      ? "stale"
      : states?.length && states.every((s) => s === "no_scoped_writes_reported")
        ? "no_scoped_writes_reported"
        : "unknown";
  const reportedOps = await client.activeOperations().catch(() => null);
  const activeOps =
    Number.isSafeInteger(reportedOps) && (reportedOps as number) >= 0 ? reportedOps : null;
  const consolidation = (await client.consolidationStatus?.().catch(() => null)) ?? null;
  const uploads = await client
    .listDocumentIds("source:upload", "all_strict")
    .catch(() => new Set<string>());
  const surveyDocs = SURVEY_DOC_IDS.filter((id) => uploads.has(id)).length;

  // Survey observability: the survey-baseline:<sha> markers Chris's re-survey mechanism writes.
  // Most recent baseline REACHABLE from HEAD wins (dead branches/rebases leave unreachable markers).
  let surveyBaseline: string | null = null;
  let surveyCommitsBehind: number | null = null;
  if (repoDir) {
    try {
      const markers = await client.listDocumentIds("source:survey-baseline", "all_strict");
      let best: { sha: string; behind: number } | undefined;
      for (const id of markers) {
        const sha = id.replace(/^survey-baseline:/, "");
        const behind = commitsSince(repoDir, sha);
        if (behind !== null && (!best || behind < best.behind)) best = { sha, behind };
      }
      if (best) {
        surveyBaseline = best.sha;
        surveyCommitsBehind = best.behind;
      }
    } catch {
      /* fail open — survey state is informational */
    }
  }

  const repoName = repoDir ? repoNameOf(repoDir) : undefined;
  // The gitlog doc id is `gitlog:<repoName>`; without a repo dir, accept any gitlog: doc.
  const gitlogPresent = repoName
    ? gitIds.has(`gitlog:${repoName}`)
    : [...gitIds].some((id) => id.startsWith("gitlog:"));
  const gitDiffDocs = [...gitIds].filter((id) => id.startsWith("git:")).length;
  const commits = repoDir ? commitCount(repoDir) : null;
  const gitDiffTarget = commits === null ? null : Math.min(DEEPEN_DIFF_TARGET, commits);

  const seedReady = gitlogPresent && (client.knowledgePagesSupported === false || pages.length > 0);
  return {
    bank,
    gitlogPresent,
    gitDiffDocs,
    gitDiffTarget,
    chatDocs: chatIds.size,
    pagesCount: pages.length,
    surveyBaseline,
    surveyDocs,
    surveyCommitsBehind,
    activeOps,
    seedReady,
    activity: activeOps === null ? "unknown" : activeOps > 0 ? "active" : "idle",
    consolidation,
    // Aggregate reported scope signals, never infer freshness from page age/existence.
    pageFreshness,
    syncScope: "seed_ingestion",
    // Older servers may explicitly lack pages; an unreportable operation count is NOT zero.
    synced: seedReady && activeOps === 0,
  };
}
