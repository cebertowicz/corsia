/**
 * The one logical owner of the attention queue's client state: the server
 * queue, which item is being answered, and — per decision identity — the
 * unsent draft and durable retry key. Activity's answer path and the plan's
 * decision rows read it.
 */
import { derived, get, writable } from "svelte/store";
import {
  fetchAttentionQueue,
  type AttentionQueue,
  type AttentionQueueItem,
} from "$lib/api/attentionQueue";
import { token } from "$lib/stores/auth";
import { answers, showNotice } from "$lib/stores/attentionAnswerState";

export interface DockDraft {
  /** Selected option id, or the reserved Other id. */
  selected: string | null;
  /** Unsent free-form text. Retired the moment a listed option is chosen. */
  other: string;
  /**
   * Pick any only: the ticked ids. `null` means untouched, which shows the
   * recommendation; an empty list is an explicit "none".
   */
  picked: string[] | null;
  /** Held across retries so a lost response can never answer twice. */
  mutationId: string | null;
}

export const EMPTY: AttentionQueue = { current: null, count: 0, items: [] };

export const queue = writable<AttentionQueue>(EMPTY);
export const goneReply = writable<{ item: AttentionQueueItem; line: string } | null>(null);

/**
 * The queue the user works: an answer that is being recorded has already left
 * it, so the dock advances at once and the count drops with it.
 */
export const visibleQueue = derived([queue, answers], ([$queue, $answers]) => {
  const items = $queue.items.filter(
    (item) => $answers[item.external_id]?.state !== "sending",
  );
  const hidden = $queue.items.length - items.length;
  return {
    current: items[0] ?? null,
    count: Math.max(0, $queue.count - hidden),
    items,
  } satisfies AttentionQueue;
});

export const expandedId = writable<string | null>(null);
/**
 * The item the user is working on. Null means the queue head. Skipping moves it
 * without closing anything: skipped items stay open on the server.
 */
export const activeId = writable<string | null>(null);
export const stale = writable(false);

const drafts = new Map<string, DockDraft>();

export function draftFor(decisionId: string): DockDraft {
  let draft = drafts.get(decisionId);
  if (!draft) {
    draft = { selected: null, picked: null, other: "", mutationId: null };
    drafts.set(decisionId, draft);
  }
  return draft;
}

export function clearDraft(decisionId: string): void {
  drafts.delete(decisionId);
}

/** Test seam: reset every module-scoped fact between cases. */
export function __reset(): void {
  queue.set(EMPTY);
  goneReply.set(null);
  answers.set({});
  showNotice(null);
  expandedId.set(null);
  activeId.set(null);
  stale.set(false);
  drafts.clear();
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

/** Which identity a write started under; a response for another one is dropped. */
export function identityEpoch(): number {
  return identityGeneration;
}

/** Everything belonging to one identity, dropped in one place. */
function clearForIdentityChange(): void {
  identityGeneration += 1;
  inFlight = null;
  queue.set(EMPTY);
  goneReply.set(null);
  answers.set({});
  showNotice(null);
  expandedId.set(null);
  activeId.set(null);
  stale.set(false);
  // Drafts and durable retry keys are per-identity too: a queued answer must
  // never be replayed under a different user's session.
  drafts.clear();
  reloadPending = false;
}

// The token store is the identity of record, so every consumer of the dock
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
 * route switch that remounts the dock cannot double-fetch.
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
      reconcile();
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

/** The item shown: the user's pick while it still waits, otherwise the head. */
export function shownItem(
  q: AttentionQueue,
  id: string | null,
): AttentionQueueItem | null {
  return q.items.find((item) => item.external_id === id) ?? q.items[0] ?? null;
}

/** The shown item moved or emptied — never leave a chip open over a stale identity. */
function reconcile(): void {
  const shown = shownItem(get(visibleQueue), get(activeId));
  activeId.set(shown ? shown.external_id : null);
  const open = get(expandedId);
  if (!open) return;
  if (!shown || shown.external_id !== open) expandedId.set(null);
}
