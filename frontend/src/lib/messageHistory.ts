import { parseUTC } from '$lib/time';
import { mergeMessageDelivery } from '$lib/stores/threads';

export interface HistoryMessage {
	id?: string;
	metadata?: { input_id?: unknown };
	created_at?: string;
	seq?: number | null;
}

function inputIdOf(message: HistoryMessage): string | null {
	return typeof message.metadata?.input_id === 'string'
		? message.metadata.input_id
		: null;
}

export function sameMessageIdentity(left: HistoryMessage, right: HistoryMessage): boolean {
	if (typeof left.id === 'string' && left.id === right.id) return true;
	const leftInputId = inputIdOf(left);
	return leftInputId !== null && leftInputId === inputIdOf(right);
}

function validSequence(value: unknown): number | null {
	return typeof value === 'number' && Number.isInteger(value) && value > 0
		? value
		: null;
}

export function compareMessageOrder(left: HistoryMessage, right: HistoryMessage): number {
	const leftSequence = validSequence(left.seq);
	const rightSequence = validSequence(right.seq);
	if (leftSequence !== null && rightSequence !== null && leftSequence !== rightSequence) {
		return leftSequence - rightSequence;
	}

	// A row that holds no sequence has not entered chat history yet: it is
	// pending, or an optimistic echo. It therefore belongs AFTER everything that
	// has a sequence, never at whatever position its timestamp implies.
	//
	// Falling through to `created_at` here is what put a queued message back at
	// the spot it was queued. `_reserve_sequence` allocates the sequence at the
	// ENDING, so a message queued mid-turn is created before the turn's later
	// output but sequenced after it -- seq 11 with a 17:43:01 timestamp sitting
	// between an assistant reply stamped 17:43:20 and the next one. Comparing
	// the two by time moves it back above the reply it was supposed to follow,
	// and it stays there until something re-sorts it with the sequence present.
	//
	// The backend states this rule outright: "a pending message (NULL seq) can
	// never render at a false position among older delivered work."
	if ((leftSequence !== null) !== (rightSequence !== null)) {
		return leftSequence !== null ? -1 : 1;
	}

	const leftTime = left.created_at ? parseUTC(left.created_at).getTime() : Number.NaN;
	const rightTime = right.created_at ? parseUTC(right.created_at).getTime() : Number.NaN;
	const leftHasTime = Number.isFinite(leftTime);
	const rightHasTime = Number.isFinite(rightTime);
	if (leftHasTime && rightHasTime && leftTime !== rightTime) return leftTime - rightTime;
	if (leftHasTime !== rightHasTime) return leftHasTime ? -1 : 1;
	return 0;
}

/**
 * Unions retained and refetched history by durable message identity, preserves
 * per-row delivery state through the supplied merger, then orders the result
 * oldest-to-newest. Positive durable sequences outrank timestamps; legacy or
 * mixed rows retain stable time/backend order rather than using random IDs.
 */
export function mergeChronologicalMessageHistory<T extends HistoryMessage>(
	current: T[],
	incoming: T[],
	merge: (existing: T, next: T) => T,
): T[] {
	const union: T[] = [];
	for (const row of [...current, ...incoming]) {
		const existingIndex = union.findIndex((candidate) => sameMessageIdentity(candidate, row));
		if (existingIndex === -1) union.push(row);
		else union[existingIndex] = merge(union[existingIndex], row);
	}
	return union.sort(compareMessageOrder);
}

/** Lifts server-shape reasoning/tool-hint fields to the row's top level for rendering. */
export function hydrateFetchedMessages(msgs: any[]): any[] {
	return msgs.map((m) => ({
		...m,
		reasoning: m.reasoning || m.metadata?.reasoning || undefined,
		toolHints: m.toolHints || m.metadata?.tool_hints || undefined,
	}));
}

/**
 * Keep every tool call already on screen when a page states fewer of them.
 *
 * A Step's calls only accumulate, so a page that omits `tool_calls` — or states
 * an empty list — is an older snapshot of the same row, not a statement that
 * the calls are gone; taking it would erase the block the reader is watching.
 * The page's own order wins where it states calls, and calls only the live row
 * holds are appended with their loaded output. A page read strips stored output
 * into `result_state: 'deferred'` and the client loads it lazily, so a call the
 * page does state keeps the output the live row already has.
 */
function mergeAdoptedToolCalls(existing: unknown, incoming: unknown): unknown {
	const prior = Array.isArray(existing) ? existing : [];
	if (!Array.isArray(incoming)) return prior.length ? prior : incoming;
	const priorById = new Map<string, any>(
		prior.map((call: any) => [String(call?.id), call]),
	);
	const merged = incoming.map((call: any) => {
		const loaded = priorById.get(String(call?.id));
		if (!loaded || typeof call?.result === 'string' || typeof loaded.result !== 'string') {
			return call;
		}
		return { ...call, result: loaded.result, result_state: loaded.result_state };
	});
	const stated = new Set(incoming.map((call: any) => String(call?.id)));
	for (const call of prior) {
		if (!stated.has(String(call?.id))) merged.push(call);
	}
	return merged;
}

/**
 * The content a row renders after adoption.
 *
 * The server owns content it states, so divergent text is replaced. Two cases
 * keep the live row: a page that is a strict prefix of it is an older snapshot
 * of the same Step (its text only grows) that arrived after the finalized
 * frame, and a row whose typewriter is still revealing it must not jump to
 * text that does not extend what is on screen.
 */
function adoptedContent(existing: any, incoming: unknown): unknown {
	const live = typeof existing?.content === 'string' ? existing.content : '';
	const page = typeof incoming === 'string' ? incoming : '';
	if (!live || page === live) return page;
	if (live.startsWith(page)) return live;
	if (existing?.typewriterPos !== undefined && !page.startsWith(live)) return live;
	return page;
}

/**
 * Merge one refetched row over a live row without thinning what is already
 * shown. The server owns every field it states; this preserves only what it
 * does not, so a page arriving mid-stream cannot empty a rendered tool block.
 */
function mergeAdoptedRow(existing: any, incoming: any): any {
	const merged: any = { ...mergeMessageDelivery(existing, incoming), _optimistic: false };
	merged.content = adoptedContent(existing, incoming?.content);
	// A page states a message's cards afresh (server-computed, live state), so an absent key means none.
	if (incoming?.cards !== undefined || existing?.cards !== undefined) {
		merged.cards = incoming?.cards;
	}
	const priorMeta = existing?.metadata;
	const nextMeta = incoming?.metadata;
	if (priorMeta && typeof priorMeta === 'object' && nextMeta && typeof nextMeta === 'object') {
		merged.metadata = {
			...priorMeta,
			...nextMeta,
			tool_calls: mergeAdoptedToolCalls(priorMeta.tool_calls, nextMeta.tool_calls),
		};
	}
	return merged;
}

/** Merges refetched rows into the live transcript, preserving each row's delivery state. */
export function mergeFetchedHistoryRows(current: any[], incoming: any[]): any[] {
	return mergeChronologicalMessageHistory(current, incoming, mergeAdoptedRow);
}

/**
 * Merges a freshly fetched first page into that thread's rows. Other threads'
 * rows survive only while `keepThread` says they are still on screen.
 */
export function mergeFetchedHistorySnapshot(
	current: any[],
	incoming: any[],
	threadId: string,
	keepThread: (threadId: string) => boolean = () => false,
): any[] {
	const others = current.filter((message) =>
		message.thread_id !== threadId && keepThread(message.thread_id));
	const own = current.filter((message) => message.thread_id === threadId);
	return [...others, ...mergeFetchedHistoryRows(own, incoming)];
}
