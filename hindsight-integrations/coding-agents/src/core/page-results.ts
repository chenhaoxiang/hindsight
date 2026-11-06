import { shapeFreshness } from "./page-freshness";

type Page = Record<string, unknown>;
const MAX_ROSTER_CHARS = 24_000;
function object(value: unknown): Page {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Invalid Hindsight page response");
  return value as Page;
}
function identity(value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 1024)
    throw new Error("Invalid Hindsight page identity");
  return value;
}
function short(value: unknown, max: number): string | null {
  return typeof value === "string" ? value.slice(0, max) : null;
}

/** Only the agent-facing roster is paged. Internal seed/refresh callers still see their full tree. */
export function boundedPageList(value: unknown, limit: number, offset: number) {
  const raw = object(value).items;
  if (!Array.isArray(raw)) throw new Error("Invalid Hindsight page roster");
  const items: Page[] = [];
  let chars = 2,
    clipped = false;
  for (const item of raw.slice(offset, offset + limit)) {
    const p = object(item);
    const description = short(p.description, 512);
    const entry = {
      id: identity(p.id),
      name: short(p.name, 256),
      ...(description ? { description } : {}),
      ...(p.folder ? { folder: short(p.folder, 256) } : {}),
      last_updated_at: short(p.last_updated_at, 64),
      freshness: p.freshness ?? shapeFreshness(),
    };
    const length = JSON.stringify(entry).length + 1;
    if (chars + length > MAX_ROSTER_CHARS) break;
    if (typeof p.description === "string" && p.description.length > 512) clipped = true;
    items.push(entry);
    chars += length;
  }
  const next = offset + items.length;
  return {
    items,
    total: raw.length,
    limit,
    offset,
    next_offset: next < raw.length ? next : null,
    truncated: clipped || next < raw.length,
  };
}

export function boundedPageSearch(hits: Page[]) {
  const pages: Page[] = [];
  let chars = 2,
    clipped = false;
  for (const hit of hits.slice(0, 20)) {
    const entry = {
      page: short(hit.name, 256),
      page_id: identity(hit.id),
      ...(hit.source_query ? { description: short(hit.source_query, 512) } : {}),
      snippet: short(hit.snippet, 1600) ?? "",
      last_updated_at: short(hit.last_updated_at, 64),
      freshness: hit.freshness ?? shapeFreshness(),
    };
    const length = JSON.stringify(entry).length + 1;
    if (chars + length > MAX_ROSTER_CHARS) break;
    if (
      (typeof hit.snippet === "string" && hit.snippet.length > 1600) ||
      (typeof hit.source_query === "string" && hit.source_query.length > 512)
    )
      clipped = true;
    pages.push(entry);
    chars += length;
  }
  return { pages, truncated: clipped || pages.length < hits.length };
}

export function boundedPageRead(value: unknown, offset: number, maxChars: number) {
  const p = object(value);
  const body = typeof p.body === "string" ? p.body : "";
  const next = Math.min(body.length, offset + maxChars);
  return {
    id: identity(p.id),
    name: short(p.name, 256),
    ...(p.description ? { description: short(p.description, 2000) } : {}),
    ...(Array.isArray(p.tags) ? { tags: p.tags.slice(0, 20).map((t) => short(t, 128)) } : {}),
    last_updated_at: short(p.last_updated_at, 64),
    freshness: p.freshness ?? shapeFreshness(),
    body: body.slice(offset, next),
    body_offset: offset,
    body_total_chars: body.length,
    body_truncated: offset > 0 || next < body.length,
    next_offset: next < body.length ? next : null,
  };
}
