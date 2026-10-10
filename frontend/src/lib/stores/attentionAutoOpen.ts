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

export type AutoOpenVerdict =
  | { state: "none" | "wait" | "dropped" }
  | { state: "open"; item: AttentionQueueItem };

/**
 * Judges the oldest decision raised in the viewed thread after the anchor.
 * Judging one, opened or dropped, moves the anchor to it, so each decision is
 * offered once; a dropped one stays in the dock as today. While the person is
 * busy or the slot is suppressed the anchor stays, so the next judge retries.
 */
export function judgeAutoOpen(
  items: AttentionQueueItem[],
  context: {
    viewedThreadId: string | null;
    expandedId: string | null;
    suppressed: boolean;
    busy: boolean;
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
