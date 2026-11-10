import type { HindsightClient, PageNode } from "./hindsight";
import { currentRequestSignal } from "./request-signal";

type Client = Pick<HindsightClient, "req" | "bankUrl">;
type Model = Record<string, unknown>;
export interface PageFreshness {
  state: "unknown" | "stale" | "refresh_failed" | "no_scoped_writes_reported";
  is_stale: boolean | null;
  last_refreshed_at: string | null;
  last_memory_seen_at: string | null;
  last_refresh_failed_at: string | null;
  observed_at: string;
  basis: "server_scoped_writes; deletions_not_observed";
  source_watermark_available: boolean;
}

function timestamp(value: unknown): string | null {
  return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value))
    ? value
    : null;
}

export function shapeFreshness(model?: Model, node?: PageNode): PageFreshness {
  const stale =
    typeof model?.is_stale === "boolean"
      ? model.is_stale
      : typeof node?.is_stale === "boolean"
        ? node.is_stale
        : null;
  const failed =
    model && "last_refresh_failed_at" in model
      ? timestamp(model.last_refresh_failed_at)
      : timestamp(node?.last_refresh_failed_at);
  const watermark = timestamp(model?.last_memory_seen_at);
  return {
    state: failed
      ? "refresh_failed"
      : stale === true
        ? "stale"
        : stale === false
          ? "no_scoped_writes_reported"
          : "unknown",
    is_stale: stale,
    last_refreshed_at:
      model && "last_refreshed_at" in model
        ? timestamp(model.last_refreshed_at)
        : timestamp(node?.timestamp),
    last_memory_seen_at: watermark,
    last_refresh_failed_at: failed,
    observed_at: new Date().toISOString(),
    basis: "server_scoped_writes; deletions_not_observed",
    source_watermark_available: watermark !== null,
  };
}

/** One bounded metadata-only batch, never N reads of model bodies/reflect traces. Beyond this
 *  page or on older/unavailable endpoints, unmatched metadata remains unknown. No shared in-flight
 *  cancellation or freshness cache: cancelling one tool cannot poison another's metadata read. */
export async function modelMetadata(client: Client): Promise<Map<string, Model>> {
  const models = new Map<string, Model>();
  try {
    const response = await client.req(
      "GET",
      client.bankUrl("/mental-models?detail=metadata&limit=1000&offset=0"),
      undefined,
      [],
      2_000
    );
    if (!response.ok) return models;
    const data = (await response.json()) as { items?: unknown };
    if (!Array.isArray(data?.items)) return models;
    for (const value of (data.items as unknown[]).slice(0, 1000)) {
      const item = value as Model | null;
      if (item && typeof item.id === "string") models.set(item.id, item);
    }
  } catch {
    currentRequestSignal()?.throwIfAborted();
    // Page content still has value when optional freshness metadata is unavailable.
  }
  return models;
}

export async function readPageFreshness(client: Client, pageId: string): Promise<PageFreshness> {
  try {
    const response = await client.req(
      "GET",
      client.bankUrl("/knowledge-base/tree"),
      undefined,
      [],
      2_000
    );
    if (!response.ok) return shapeFreshness();
    const tree = (await response.json()) as { roots?: PageNode[] };
    const stack = Array.isArray(tree?.roots) ? [...tree.roots] : [];
    let node: PageNode | undefined;
    while (stack.length) {
      const item = stack.pop()!;
      if (item?.kind === "page" && item.id === pageId) {
        node = item;
        break;
      }
      if (Array.isArray(item?.children)) stack.push(...item.children);
    }
    if (!node?.mental_model_id) return shapeFreshness(undefined, node);
    try {
      const response = await client.req(
        "GET",
        client.bankUrl(
          `/mental-models/${encodeURIComponent(node.mental_model_id)}?detail=metadata`
        ),
        undefined,
        [],
        2_000
      );
      if (!response.ok) return shapeFreshness(undefined, node);
      const model = (await response.json()) as Model;
      return shapeFreshness(model?.id === node.mental_model_id ? model : undefined, node);
    } catch {
      currentRequestSignal()?.throwIfAborted();
      return shapeFreshness(undefined, node);
    }
  } catch {
    currentRequestSignal()?.throwIfAborted();
    return shapeFreshness();
  }
}
