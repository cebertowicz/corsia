/**
 * What a person was answering, captured invisibly for the message they send.
 *
 * While a message is being composed (from the first input in the composer until
 * it is sent), the message rows that were on screen at any moment accumulate
 * here; at send they become merged seq ranges with their visible-row counts.
 * The first unread message is read at send from this visit's divider. Nothing
 * here is shown to the person.
 */
import { get } from 'svelte/store';
import { visitDividers } from './threadUpdateCards';

/** `[from_seq, to_seq, rows_shown]`: rows inside a collapsed block are not in the count. */
export type InViewRange = [number, number, number];

export interface ReplyContext {
	in_view?: InViewRange[];
	first_unread?: { seq: number; truncated: boolean };
}

function mergeSpans(spans: ReadonlyArray<readonly [number, number]>): Array<[number, number]> {
	const merged: Array<[number, number]> = [];
	for (const [from, to] of [...spans].sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
		const last = merged.at(-1);
		if (last && from <= last[1] + 1) last[1] = Math.max(last[1], to);
		else merged.push([from, to]);
	}
	return merged;
}

/**
 * Join spans that overlap or touch, counting only the rows actually seen.
 * A span covers every row between its first and last visible row, so one that
 * crosses a collapsed work block can be much wider than its row count.
 */
export function mergeInView(spans: ReadonlyArray<readonly [number, number]>, seen: ReadonlySet<number>): InViewRange[] {
	return mergeSpans(spans).map(([from, to]) => {
		let shown = 0;
		for (const seq of seen) if (seq >= from && seq <= to) shown++;
		return [from, to, shown];
	});
}

let composingThread: string | null = null;
let spans: Array<[number, number]> = [];
let seen = new Set<number>();
/** The rows on screen right now, whether or not anyone is composing. */
let visibleSeqs: number[] = [];

function record(seqs: readonly number[]): void {
	if (seqs.length === 0) return;
	spans.push([Math.min(...seqs), Math.max(...seqs)]);
	for (const seq of seqs) seen.add(seq);
	// Keep the list short on a long scroll: the merge is lossless.
	spans = mergeSpans(spans);
}

/** The observer reports the seqs of every row on screen after each change. */
export function setVisibleRows(seqs: readonly number[]): void {
	visibleSeqs = [...seqs];
	if (composingThread) record(visibleSeqs);
}

/** The first input in the composer starts the window; the rows already on screen count. */
export function beginComposing(threadId: string): void {
	if (composingThread === threadId) return;
	composingThread = threadId;
	spans = [];
	seen = new Set();
	record(visibleSeqs);
}

/** A send or an emptied composer ends the window; the next first input starts a fresh one. */
export function cancelComposing(): void {
	composingThread = null;
	spans = [];
	seen = new Set();
}

/**
 * What a composer holds changed. Only a composer for the thread that owns the
 * window can end it, so another thread's empty composer never cancels this one.
 */
export function noteComposerText(threadId: string, text: string): void {
	if (text.trim()) beginComposing(threadId);
	else if (composingThread === threadId) cancelComposing();
}

/**
 * The reply context for the send of `threadId`, or null when there is none.
 * Ends the composing window, so the next message starts from nothing.
 */
export function takeReplyContext(threadId: string): ReplyContext | null {
	const context: ReplyContext = {};
	if (composingThread === threadId) {
		const ranges = mergeInView(spans, seen);
		if (ranges.length > 0) context.in_view = ranges;
	}
	cancelComposing();
	const divider = get(visitDividers)[threadId]?.divider;
	if (divider && typeof divider.first_seq === 'number') {
		context.first_unread = { seq: divider.first_seq, truncated: divider.truncated === true };
	}
	return context.in_view || context.first_unread ? context : null;
}
