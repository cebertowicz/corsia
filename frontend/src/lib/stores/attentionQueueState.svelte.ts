/**
 * The one logical owner of the attention queue's client state: the server
 * queue and whether the last read failed. The plan's decision rows read it;
 * Activity keeps its own read of the same server queue.
 */
import { derived, get, writable } from "svelte/store";
import { fetchAttentionQueue, type AttentionQueue } from "$lib/api/attentionQueue";
import { token } from "$lib/stores/auth";

export const EMPTY: AttentionQueue = { current: null, count: 0, items: [] };

export const queue = writable<AttentionQueue>(EMPTY);

/**
 * The queue the user works: the server's order, with its head as `current`.
 */
export const visibleQueue = derived(queue, ($queue) => {
  return {
    current: $queue.items[0] ?? null,
    count: $queue.count,
    items: $queue.items,
  } satisfies AttentionQueue;
});

export const stale = writable(false);

/** Test seam: reset every module-scoped fact between cases. */
export function __reset(): void {
  queue.set(EMPTY);
  stale.set(false);
  inFlight = null;
  reloadPending = false;
  identityGeneration += 1;
}

let inFlight: Promise<void> | null = null;
let reloadPending = false;

/**
 * Bumped whenever the signed-in identity changes. A response already in flight
 * for the previous identity is inadmissible: without this, a slow read issued
 * before sign-out could repopulate the queue afterwards.
 */
let identityGeneration = 0;

/** Everything belonging to one identity, dropped in one place. */
function clearForIdentityChange(): void {
  identityGeneration += 1;
  inFlight = null;
  queue.set(EMPTY);
  stale.set(false);
  reloadPending = false;
}

// The token store is the identity of record, so every consumer of the queue
// clears together rather than each surface remembering to.
let sawToken = false;
token.subscribe((value) => {
  if (!value) {
    if (sawToken) clearForIdentityChange();
    sawToken = false;
    return;
  }
  if (!sawToken) sawToken = true;
});

export type DecisionCheck = "live" | "empty" | "unavailable";

/**
 * Re-reads the queue and says whether a decision still waits (with no id, whether any does), so a plan row that
 * opens Activity at it can say it closed instead. `unavailable`: the read failed.
 */
export async function checkDecision(itemId?: string): Promise<DecisionCheck> {
  await reload();
  const items = get(visibleQueue);
  const current = itemId ? items.items.find((item) => item.external_id === itemId) ?? null : items.current;
  if (!current) return get(stale) ? "unavailable" : "empty";
  return "live";
}

/**
 * Re-reads the canonical queue. Concurrent callers share one request, so a
 * route switch that remounts a consumer cannot double-fetch.
 */
export async function reload(): Promise<void> {
  if (inFlight) {
    reloadPending = true;
    return inFlight;
  }
  const bearer = get(token);
  if (!bearer) {
    // No identity means nothing may be shown. Returning early here is what
    // left the previous user's question on screen after sign-out.
    clearForIdentityChange();
    return;
  }
  const generation = identityGeneration;
  inFlight = (async () => {
    try {
      const next = await fetchAttentionQueue();
      if (generation !== identityGeneration) return;
      if (!Array.isArray(next?.items)) throw new Error("malformed queue");
      queue.set(next);
      stale.set(false);
    } catch {
      if (generation !== identityGeneration) return;
      stale.set(true);
    } finally {
      if (generation !== identityGeneration) return;
      inFlight = null;
      if (reloadPending) {
        reloadPending = false;
        void reload();
      }
    }
  })();
  return inFlight;
}
