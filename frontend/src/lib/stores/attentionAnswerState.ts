import { writable } from "svelte/store";

/**
 * Where one decision's answer stands after the user sent it. `sending` is an
 * answer shown as recorded while its write is in flight; `failed` is that
 * answer rolled back, with the specific reason the chip shows. The unsent
 * draft and its retry key stay in `drafts`, so a retry carries the same key.
 */
export type AnswerState =
  | { state: "sending" }
  | {
      state: "failed";
      kind: "rejected" | "stale" | "unauthorized" | "error";
      message: string;
    };

export const answers = writable<Record<string, AnswerState>>({});

/**
 * One sentence about an answer that never landed because its question had
 * already left the queue. The dock shows it where the receipt shows; it clears
 * itself after a few seconds, and sooner on the next answer or identity change.
 */
export const answerNotice = writable<string | null>(null);

export const NOTICE_MS = 8000;
let noticeTimer: ReturnType<typeof setTimeout> | undefined;

export function showNotice(message: string | null): void {
  clearTimeout(noticeTimer);
  answerNotice.set(message);
  if (message) noticeTimer = setTimeout(() => answerNotice.set(null), NOTICE_MS);
}

export function setAnswer(decisionId: string, next: AnswerState | null): void {
  answers.update((all) => {
    const { [decisionId]: _drop, ...rest } = all;
    return next ? { ...rest, [decisionId]: next } : rest;
  });
}
