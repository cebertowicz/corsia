/**
 * A decision raised by the thread being viewed opens on its own.
 *
 * "New" is decided by the server's `raised_at` against one per-view anchor, so
 * no lifecycle edge (mount, reconnect, sign-in, returning to a thread) needs
 * its own case. The dock store feeds each load in and applies the verdict
 * through its single open path, `expand`.
 */
import type { AttentionQueueItem, DecisionDetail } from "$lib/api/attentionQueue";

/** `anchor` is the newest `raised_at` (ms) already accounted for in `threadId`. */
let view: { threadId: string; anchor: number | null } | null = null;

/** Sign-out, dock unmount and test resets: the next load re-anchors. */
export function forgetAutoOpen(): void {
  view = null;
}

function raisedAt(item: AttentionQueueItem): number | null {
  if (item.renderer !== "decision") return null;
  const ms = Date.parse((item.detail as DecisionDetail).raised_at ?? "");
  return Number.isNaN(ms) ? null : ms;
}

function fromThread(
  items: AttentionQueueItem[],
  threadId: string,
): AttentionQueueItem[] {
  // A hidden worker's decision carries the worker's id, so it never matches.
  return items.filter(
    (item) =>
      item.renderer === "decision" && item.detail.source?.thread_id === threadId,
  );
}

/**
 * Called when a queue load lands. The first load for a viewed thread only sets
 * the anchor, so whatever was already waiting there never opens.
 */
export function noteLoad(
  items: AttentionQueueItem[],
  viewedThreadId: string | null,
): void {
  if (!viewedThreadId) {
    view = null;
    return;
  }
  if (view?.threadId === viewedThreadId) return;
  const stamps = fromThread(items, viewedThreadId)
    .map(raisedAt)
    .filter((ms): ms is number => ms !== null);
  view = {
    threadId: viewedThreadId,
    anchor: stamps.length ? Math.max(...stamps) : null,
  };
}

/** About 250 words a minute: the pace a person takes to read a message through. */
const READ_MS_PER_WORD = 240;
/** However much text came before, a decision never waits longer than this after it was raised. */
const MAX_READ_HOLD_MS = 45_000;

export interface ReadableMessage {
  thread_id: string;
  role: string;
  content: string;
  created_at: string;
}

/**
 * When the person can be expected to have finished reading what the agent said
 * just before a decision was raised: the assistant messages since their last
 * one, read one after another, each starting when it landed or when the
 * previous one is done. A slow model that streamed for longer than the text
 * takes to read has already used up the time, so nothing is added for it.
 */
export function readingEndsAt(
  messages: ReadableMessage[],
  threadId: string,
  raisedAtMs: number,
): number {
  const unread: { at: number; words: number }[] = [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.thread_id !== threadId) continue;
    if (message.role === "user") break;
    const at = Date.parse(message.created_at);
    if (message.role !== "assistant" || Number.isNaN(at) || at > raisedAtMs) continue;
    unread.unshift({ at, words: message.content.split(/\s+/).filter(Boolean).length });
  }
  let done = 0;
  for (const { at, words } of unread) done = Math.max(done, at) + words * READ_MS_PER_WORD;
  return Math.min(done, raisedAtMs + MAX_READ_HOLD_MS);
}

export type AutoOpenVerdict =
  | { state: "none" | "dropped" }
  | { state: "wait"; retryInMs?: number }
  | { state: "open"; item: AttentionQueueItem };

/**
 * Judges the oldest decision raised in the viewed thread after the anchor.
 * Judging one, opened or dropped, moves the anchor to it, so each decision is
 * offered once; a dropped one stays in the dock as today. While the person is
 * busy, the slot is suppressed or the text before it is still being read, the
 * anchor stays, so the next judge retries.
 */
export function judgeAutoOpen(
  items: AttentionQueueItem[],
  context: {
    viewedThreadId: string | null;
    expandedId: string | null;
    suppressed: boolean;
    busy: boolean;
    /** The viewed thread's transcript, so a decision waits for the text before it to be read. */
    messages?: ReadableMessage[];
    now?: number;
  },
): AutoOpenVerdict {
  if (!view || view.threadId !== context.viewedThreadId) return { state: "none" };
  const { anchor } = view;
  const candidate = fromThread(items, view.threadId).find((item) => {
    const at = raisedAt(item);
    return at !== null && (anchor === null || at > anchor);
  });
  if (!candidate) return { state: "none" };
  if (context.expandedId === null && (context.suppressed || context.busy)) {
    return { state: "wait" };
  }
  if (context.expandedId === null && context.messages) {
    const readyIn =
      readingEndsAt(context.messages, view.threadId, raisedAt(candidate)!) -
      (context.now ?? Date.now());
    if (readyIn > 0) return { state: "wait", retryInMs: readyIn };
  }
  view.anchor = raisedAt(candidate);
  return context.expandedId === null
    ? { state: "open", item: candidate }
    : { state: "dropped" };
}

const NON_TEXT_INPUTS = new Set([
  "button", "checkbox", "color", "file", "image", "radio", "range", "reset", "submit",
]);

/**
 * A focused text field holding an unsent draft (covers a raised mobile
 * keyboard). An empty field is not busy: the composer keeps focus after a send,
 * so focus alone would hold every decision off for good.
 */
export function holdsDraft(el: Element | null): boolean {
  if (!el) return false;
  if (el instanceof HTMLTextAreaElement) return el.value.trim() !== "";
  if (el instanceof HTMLInputElement) {
    return !NON_TEXT_INPUTS.has(el.type) && el.value.trim() !== "";
  }
  const editable = (el as HTMLElement).closest?.("[contenteditable]");
  return (
    !!editable &&
    editable.getAttribute("contenteditable") !== "false" &&
    (editable.textContent ?? "").trim() !== ""
  );
}
