/**
 * The one logical owner of dock state.
 *
 * The chat attention slot is the sole mount point. This module holds everything
 * that must survive a transient remount: the server queue, which item is
 * expanded, and — per decision identity — the unsent draft and durable retry key.
 *
 * The pattern is the one `HostAccessLeaseControl` already uses: a module-scoped
 * store for the facts that must outlive any single mount.
 */
import { derived, get, writable } from "svelte/store";
import {
  fetchAttentionQueue,
  type AttentionQueue,
  type AttentionQueueItem,
} from "$lib/api/attentionQueue";
import { token } from "$lib/stores/auth";
import { answers, showNotice } from "$lib/stores/attentionAnswerState";
import { activeThreadId } from "$lib/stores/threads";
import { toasts } from "$lib/stores/toast";
import { forgetAutoOpen, noteLoad } from "$lib/stores/attentionAutoOpen";

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
    reply_items: $queue.reply_items,
  } satisfies AttentionQueue;
});

export const expandedId = writable<string | null>(null);
/**
 * The item the user is working on. Null means the queue head. Skipping moves it
 * without closing anything: skipped items stay open on the server.
 */
export const activeId = writable<string | null>(null);
export const stale = writable(false);

/**
 * Bumped when another surface (a plan's blocking indicator) asks the canonical
 * dock to open and take focus. A counter, not a boolean, so two consecutive
 * requests for the same item are both honoured.
 */
export const focusRequest = writable(0);

export type FocusRequestResult = "requested" | "empty" | "unavailable";

/** Refresh canonical queue truth, then ask the dock to open; an optional id preserves a selected row. */
export async function requestFocus(itemId?: string): Promise<FocusRequestResult> {
  await reload();
  const items = get(visibleQueue);
  const current = itemId ? items.items.find((item) => item.external_id === itemId) ?? null : items.current;
  if (!current) return get(stale) ? "unavailable" : "empty";
  activeId.set(current.external_id);
  focusRequest.update((n) => n + 1);
  return "requested";
}

/**
 * The thread whose dock should expand the head decision when it mounts there.
 * Set only after Activity has opened that thread, and consumed (or dropped) by
 * the first dock that mounts, so a request can never outlive the open it
 * belongs to.
 */
export const focusTarget = writable<string | null>(null);

export type OpenFromActivityResult =
  | "opened"
  | "empty"
  | "unavailable"
  | "no-thread"
  | "failed";

/**
 * Activity renders no dock, so a decision opens in the thread that owns it:
 * read the queue once, open the thread, then ask the dock mounting there to
 * expand. `threadId` overrides the head's thread (a plan row's own thread).
 * Nothing is requested unless the thread actually opened.
 */
export async function openDecisionFromActivity(
  openThread: (threadId: string) => void | Promise<void>,
  threadId: string | null = null,
): Promise<OpenFromActivityResult> {
  await reload();
  const head = get(visibleQueue).current;
  if (!head) return get(stale) ? "unavailable" : "empty";
  const headThread = head.detail.source?.thread_id ?? null;
  const target = threadId ?? headThread;
  if (!target) return "no-thread";
  try {
    await openThread(target);
  } catch {
    return "failed";
  }
  if (get(activeThreadId) !== target) return "failed";
  // A row's thread that is not the head's owns a different decision: open it,
  // but never expand the head's decision over it.
  if (target === headThread) focusTarget.set(target);
  return "opened";
}

const OPEN_NOTICE: Record<Exclude<OpenFromActivityResult, "opened">, string> = {
  empty: "That decision no longer needs you.",
  unavailable: "Couldn't load your decisions. Try again.",
  "no-thread": "Couldn't find the thread for that decision.",
  failed: "Couldn't open the thread for that decision.",
};

/** Says why a decision did not open; a successful open needs no message. */
export function reportOpenDecision(result: OpenFromActivityResult): void {
  if (result !== "opened") toasts.show(OPEN_NOTICE[result], "error");
}

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
  answers.set({});
  showNotice(null);
  expandedId.set(null);
  activeId.set(null);
  stale.set(false);
  focusRequest.set(0);
  focusTarget.set(null);
  forgetAutoOpen();
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
  answers.set({});
  showNotice(null);
  expandedId.set(null);
  focusTarget.set(null);
  activeId.set(null);
  stale.set(false);
  forgetAutoOpen();
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
      const next = await fetchAttentionQueue(bearer);
      if (generation !== identityGeneration) return;
      if (!Array.isArray(next?.items)) throw new Error("malformed queue");
      queue.set(next);
      stale.set(false);
      reconcile();
      noteLoad(next.items, get(activeThreadId));
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

/** Advance to the next waiting item, wrapping past the last. */
export function skip(): void {
  const q = get(visibleQueue);
  if (q.items.length < 2) return;
  const here = q.items.findIndex((item) => item.external_id === get(activeId));
  const next = q.items[(here + 1) % q.items.length];
  // An open drawer stays open on the next item; a collapsed bar stays collapsed.
  if (get(expandedId)) expandedId.set(next.external_id);
  activeId.set(next.external_id);
}

/** The shown item moved or emptied — never leave a chip open over a stale identity. */
function reconcile(): void {
  const shown = shownItem(get(visibleQueue), get(activeId));
  activeId.set(shown ? shown.external_id : null);
  const open = get(expandedId);
  if (!open) return;
  if (!shown || shown.external_id !== open) expandedId.set(null);
}

export function expand(
  item: AttentionQueueItem | null,
  suppressed: boolean,
): void {
  if (!item || suppressed) return;
  expandedId.set(item.external_id);
}

export function collapse(): void {
  expandedId.set(null);
}
