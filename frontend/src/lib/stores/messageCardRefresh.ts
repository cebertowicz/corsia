/**
 * Keeps the artifact cards on loaded messages current, with no polling.
 *
 * A card is computed when its message is read, so its round state and title go stale
 * when a hold clears (`plans_changed`) or an artifact is renamed or retired
 * (`artifacts_changed`), and a message gains cards when its turn ends. Each of those
 * re-reads the affected messages through the range read the transcript already uses,
 * and merges them over the loaded rows.
 */
import { get } from 'svelte/store';
import { on } from '$lib/ws';
import { hydrateFetchedMessages, mergeFetchedHistoryRows } from '$lib/messageHistory';
import { token } from './auth';
import { activeThreadId, messages, type Message } from './threads';

const DEBOUNCE_MS = 400;
/** The newest carded messages re-read per nudge; older ones refresh when scrolled to and re-read. */
const MAX_CARDED = 12;
const TURN_RANGE_LIMIT = 50;

type Range = { from: string; to: string };

/** The messages to re-read: those showing cards, and after a turn ends, that turn's own steps. */
export function rangesToRefresh(rows: Message[], threadId: string, turnEnded: boolean): Range[] {
	const own = rows.filter((row) => row.thread_id === threadId && !row._optimistic);
	const ranges: Range[] = [];
	let turnFirst = -1;
	if (turnEnded) {
		const lastUser = own.map((row) => row.role).lastIndexOf('user');
		turnFirst = own.findIndex((row, index) => index > lastUser && row.role === 'assistant');
		const last = own.at(-1);
		if (turnFirst >= 0 && last) ranges.push({ from: own[turnFirst].id, to: last.id });
	}
	own.filter((row, index) => row.role === 'assistant' && Array.isArray(row.cards) && (turnFirst < 0 || index < turnFirst))
		.slice(-MAX_CARDED)
		.forEach((row) => ranges.push({ from: row.id, to: row.id }));
	return ranges;
}

async function read(threadId: string, bearer: string, range: Range): Promise<unknown[]> {
	const params = new URLSearchParams({
		limit: String(TURN_RANGE_LIMIT), from_id: range.from, to_id: range.to, token: bearer,
	});
	const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/messages?${params}`);
	if (!res.ok) return [];
	const page = await res.json();
	return Array.isArray(page?.messages) ? page.messages : [];
}

let pending: ReturnType<typeof setTimeout> | null = null;
let turnEnded = false;

async function refresh(): Promise<void> {
	const threadId = get(activeThreadId);
	const bearer = get(token);
	const ended = turnEnded;
	turnEnded = false;
	if (!threadId || !bearer) return;
	const ranges = rangesToRefresh(get(messages), threadId, ended);
	if (!ranges.length) return;
	try {
		const pages = await Promise.all(ranges.map((range) => read(threadId, bearer, range)));
		// A read that returns after the thread on screen changed is dropped.
		if (get(activeThreadId) !== threadId) return;
		const rows = pages.flat();
		if (rows.length) messages.update((current) => mergeFetchedHistoryRows(current, hydrateFetchedMessages(rows as never)));
	} catch (error) {
		console.error('[message cards] refresh failed', threadId, error);
	}
}

function schedule(): void {
	if (pending) clearTimeout(pending);
	pending = setTimeout(() => {
		pending = null;
		void refresh();
	}, DEBOUNCE_MS);
}

let watching = false;

/** Subscribes once per page. */
export function watchMessageCards(): void {
	if (watching) return;
	watching = true;
	on('plans_changed', schedule);
	on('artifacts_changed', schedule);
	on('turn_complete', () => {
		turnEnded = true;
		schedule();
	});
}
