import { z } from "zod";
import type { HindsightClient } from "./hindsight";

type ReadClient = Pick<HindsightClient, "recallFactResponse" | "recentFactResponse">;
const FACT_TYPES = ["world", "experience", "observation"] as const;
type FactType = (typeof FACT_TYPES)[number];
const filters = {
  types: z.array(z.enum(FACT_TYPES)).min(1).max(3).default(["world", "experience"]),
  tags: z.array(z.string().min(1).max(128)).max(20).optional(),
  tags_match: z.enum(["all_strict", "any_strict", "exact"]).default("all_strict"),
};
export const recallInput = {
  query: z.string().trim().min(1).max(2000),
  ...filters,
  limit: z.number().int().min(1).max(20).default(8),
  max_tokens: z.number().int().min(128).max(4000).default(2000),
};
export const recentInput = {
  since: z.iso.datetime({ offset: true }),
  until: z.iso.datetime({ offset: true }).optional(),
  ...filters,
  limit: z.number().int().min(1).max(20).default(10),
};
const recallArgs = z.object(recallInput).strict();
const recentArgs = z.object(recentInput).strict();

export class MemoryReadUnavailableError extends Error {
  readonly code = "memory_read_unavailable";
}

interface Fact {
  id: string;
  fact_type: FactType;
  text: string;
  document_id: string | null;
  chunk_id: string | null;
  context: string | null;
  tags: string[];
  metadata: Record<string, unknown>;
  occurred_start: string | null;
  occurred_end: string | null;
  mentioned_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  consolidated_at: string | null;
  consolidation_failed_at: string | null;
  truncated: boolean;
}

type Row = Record<string, unknown>;
function rows(value: unknown, key: string): Row[] {
  const items = (value as Row | null)?.[key];
  if (!Array.isArray(items) || items.some((x) => !x || typeof x !== "object" || Array.isArray(x)))
    throw new Error("Invalid Hindsight memory response");
  return items as Row[];
}

async function readJson(response: Response): Promise<unknown> {
  if (response.status === 404) {
    const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    if (typeof body?.detail === "string" && body.detail.trim().toLowerCase() === "not found")
      throw new MemoryReadUnavailableError(
        "The server does not expose this memory-read capability"
      );
    throw new Error("Hindsight memory bank not found or memory endpoint unavailable");
  }
  if (!response.ok) throw new Error("Hindsight memory read unavailable");
  return response.json();
}

function matchingTags(row: Row, tags: string[] | undefined, mode: string): boolean {
  const actual = row.tags;
  if (actual != null && (!Array.isArray(actual) || actual.some((t) => typeof t !== "string")))
    throw new Error("Invalid Hindsight memory tags");
  const set = new Set((actual ?? []) as string[]);
  const wanted = new Set(tags ?? []);
  if (mode === "exact") return set.size === wanted.size && [...wanted].every((t) => set.has(t));
  if (!wanted.size) return true;
  return mode === "any_strict"
    ? [...wanted].some((t) => set.has(t))
    : [...wanted].every((t) => set.has(t));
}

/** Preserve provenance without handing arbitrary metadata/body sizes to the host context.
 *  Truncation is explicit, including metadata reduced to previews; identifiers are never shortened. */
function shapeFact(
  row: Row,
  allowed: readonly string[],
  tags: string[] | undefined,
  mode: string
): Fact {
  const type = row.type ?? row.fact_type;
  if (
    typeof row.id !== "string" ||
    !row.id ||
    row.id.length > 1024 ||
    typeof row.text !== "string" ||
    !FACT_TYPES.includes(type as FactType) ||
    !allowed.includes(type as string)
  )
    throw new Error("Invalid or unexpected Hindsight fact type/identity");
  if (!matchingTags(row, tags, mode))
    throw new Error("Hindsight memory response did not honor the requested tag boundary");
  let truncated = false;
  const text = (value: unknown, max: number): string | null => {
    if (typeof value !== "string") return null;
    if (value.length > max) {
      truncated = true;
      return value.slice(0, max);
    }
    return value;
  };
  const id = (value: unknown): string | null => {
    if (value == null) return null;
    if (typeof value !== "string" || value.length > 2048)
      throw new Error("Invalid Hindsight provenance identifier");
    return value;
  };
  const date = (key: string): string | null =>
    typeof row[key] === "string" &&
    (row[key] as string).length <= 64 &&
    Number.isFinite(Date.parse(row[key] as string))
      ? (row[key] as string)
      : null;
  const metadata: Record<string, unknown> = Object.create(null);
  if (row.metadata && typeof row.metadata === "object" && !Array.isArray(row.metadata)) {
    const entries = Object.entries(row.metadata);
    if (entries.length > 12) truncated = true;
    for (const [key, value] of entries.slice(0, 12)) {
      if (key.length > 128) {
        truncated = true;
        continue;
      }
      const encoded = JSON.stringify(value) ?? "null";
      if (encoded.length > 256) {
        metadata[key] = { truncated: true, preview: encoded.slice(0, 160) };
        truncated = true;
      } else metadata[key] = value;
    }
  }
  const rawTags = (row.tags ?? []) as string[];
  if (rawTags.length > 20) truncated = true;
  const result: Fact = {
    id: row.id,
    fact_type: type as FactType,
    text: text(row.text, 4000)!,
    document_id: id(row.document_id),
    chunk_id: id(row.chunk_id),
    context: text(row.context, 600),
    tags: rawTags.slice(0, 20).map((t) => text(t, 128)!),
    metadata,
    occurred_start: date("occurred_start"),
    occurred_end: date("occurred_end"),
    mentioned_at: date("mentioned_at"),
    created_at: date("created_at"),
    updated_at: date("updated_at"),
    consolidated_at: date("consolidated_at"),
    consolidation_failed_at: date("consolidation_failed_at"),
    truncated: false,
  };
  result.truncated = truncated;
  return result;
}

/** Fixed result budget in addition to API token/row limits. Omitted facts remain omitted, never
 *  represented as a complete result set. The bound is for memory records; tool guidance is fixed. */
function boundFacts(facts: Fact[], limit: number) {
  const memories: Fact[] = [];
  let chars = 2;
  for (const fact of facts.slice(0, limit)) {
    const length = JSON.stringify(fact).length + 1;
    if (chars + length > 24_000) break;
    memories.push(fact);
    chars += length;
  }
  return {
    memories,
    truncated: memories.length < facts.length || memories.some((f) => f.truncated),
  };
}

export async function recallFacts(client: ReadClient, input: unknown) {
  const args = recallArgs.parse(input);
  const response = await client.recallFactResponse({
    query: args.query,
    types: [...new Set(args.types)],
    budget: "low",
    max_tokens: args.max_tokens,
    tags: args.tags,
    tags_match: args.tags_match,
    prefer_observations: false,
    include: { entities: null },
  });
  const data = rows(await readJson(response), "results");
  const facts = data
    .slice(0, args.limit)
    .map((row) => shapeFact(row, args.types, args.tags, args.tags_match));
  const bounded = boundFacts(facts, args.limit);
  return {
    source: "raw_recall",
    ...bounded,
    truncated: bounded.truncated || data.length > args.limit,
    types: args.types,
    tags: args.tags ?? [],
    tags_match: args.tags_match,
    temporal_basis: "event timestamps are provenance, not ingestion-time filters",
  };
}

/** Match the API's Python/Postgres microsecond precision, including offsets. Date.parse gives
 *  the whole millisecond; restore its three discarded fractional digits without rounding or
 *  relabelling the returned ISO string. Finer input fractions truncate as Python's parser does. */
function writeTimeMicros(value: string): bigint | null {
  const millis = Date.parse(value);
  if (!Number.isFinite(millis) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const fraction = value.match(/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/)?.[1] ?? "";
  const remainder = fraction.slice(3, 6).padEnd(3, "0");
  return BigInt(millis) * 1000n + BigInt(remainder);
}

export async function recentFacts(client: ReadClient, input: unknown) {
  const args = recentArgs.parse(input);
  const until = args.until ?? new Date().toISOString();
  const start = writeTimeMicros(args.since),
    end = writeTimeMicros(until);
  if (start === null || end === null || end <= start)
    throw new Error("until must be later than since");
  const types = [...new Set(args.types)];
  // One top-N page per type is sufficient for the merged top N. Never scan history or let a
  // burst of synthesized observations displace every unconsolidated source fact by default.
  const pages = await Promise.all(
    types.map(async (type) => {
      const query = new URLSearchParams({
        type,
        time_field: "updated_at",
        start_date: args.since,
        end_date: until,
        limit: String(args.limit),
        offset: "0",
        tags_match: args.tags_match,
      });
      for (const tag of args.tags ?? []) query.append("tags", tag);
      const body = await readJson(await client.recentFactResponse(query));
      const items = rows(body, "items");
      const total = (body as Row).total;
      if (
        !Number.isSafeInteger(total) ||
        (total as number) < items.length ||
        items.length > args.limit
      )
        throw new Error("Invalid Hindsight recent-memory totals");
      const facts = items.map((row) => shapeFact(row, [type], args.tags, args.tags_match));
      for (const fact of facts) {
        const updated = fact.updated_at === null ? null : writeTimeMicros(fact.updated_at);
        if (updated === null || updated < start || updated >= end)
          throw new MemoryReadUnavailableError(
            "The server's updated-at window cannot be verified; no event-time substitute was used"
          );
      }
      return { facts, hasMore: (total as number) > facts.length };
    })
  );
  const facts = pages
    .flatMap((p) => p.facts)
    .sort((a, b) => {
      const left = writeTimeMicros(a.updated_at!)!,
        right = writeTimeMicros(b.updated_at!)!;
      return left > right ? -1 : left < right ? 1 : a.id.localeCompare(b.id);
    });
  const bounded = boundFacts(facts, args.limit);
  return {
    source: "recent_list",
    ...bounded,
    truncated: bounded.truncated || pages.some((p) => p.hasMore),
    types,
    tags: args.tags ?? [],
    tags_match: args.tags_match,
    time_field: "updated_at",
    window: { since_inclusive: args.since, until_exclusive: until },
    temporal_basis: "memory row write time; not event time or document creation time",
  };
}
