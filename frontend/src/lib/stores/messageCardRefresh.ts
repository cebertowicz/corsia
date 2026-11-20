/**
 * Keeps the artifact cards on loaded messages current, with no polling.
 *
 * A card is computed when its message is read, so its round state and title go stale
 * when a hold clears (`plans_changed`) or an artifact is renamed or retired
 * (`artifacts_changed`), and a message gains cards when its turn ends. Each nudge
 * re-reads the affected messages through bounded server reads and merges them over
 * the loaded rows.
 */
import { get } from 'svelte/store';
import { on } from '$lib/ws';
import { hydrateFetchedMessages, mergeFetchedHistoryRows } from '$lib/messageHistory';
import { token } from './auth';
import { activeThreadId, messages, type Message } from './threads';
import { apiFetch } from '$lib/api/request';
// The server's own file: one definition of both bounds, read by the batch route too.
import limits from '../../../../backend/api/card_refresh_limits.json';
const DEBOUNCE_MS = 400;
/** The newest carded messages re-read per nudge; older ones refresh when scrolled to and re-read. */
const MAX_CARDED = limits.max_carded;
const TURN_RANGE_LIMIT = limits.turn_range_limit;

type Range = { from: string; to: string };
type Selection = { messageIds: string[]; turn: Range | null };

export function selectionToRefresh(rows: Message[], threadId: string, turnEnded: boolean): Selection {
	const own = rows.filter((row) => row.thread_id === threadId && !row._optimistic);
	let turnFirst = -1;
	let turn: Range | null = null;
	if (turnEnded) {
		const lastUser = own.map((row) => row.role).lastIndexOf('user');
		turnFirst = own.findIndex((row, index) => index > lastUser && row.role === 'assistant');
		const steps = turnFirst >= 0 ? own.slice(turnFirst).slice(-TURN_RANGE_LIMIT) : [];
		if (steps.length) turn = { from: steps[0].id, to: steps.at(-1)!.id };
	}
	const messageIds = own.filter((row, index) => (
		row.role === 'assistant' && Array.isArray(row.cards) && (turnFirst < 0 || index < turnFirst)
	)).slice(-MAX_CARDED).map((row) => row.id);
	return { messageIds, turn };
}

async function read(threadId: string, selection: Selection): Promise<unknown[]> {
	const res = await apiFetch(`/api/threads/${encodeURIComponent(threadId)}/messages/refresh`, {
		method: 'POST', sessionInAddress: true, headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			message_ids: selection.messageIds,
			turn_range: selection.turn ? { from_id: selection.turn.from, to_id: selection.turn.to } : null,
		}),
	});
	if (!res.ok) return [];
	const page = await res.json();
	return Array.isArray(page?.messages) ? page.messages : [];
}

let pending: ReturnType<typeof setTimeout> | null = null;
let turnEnded = false;
let inFlight = false;
let trailing = false;

async function refresh(): Promise<void> {
	const threadId = get(activeThreadId);
	const ended = turnEnded;
	turnEnded = false;
	if (!threadId || !get(token)) return;
	const selection = selectionToRefresh(get(messages), threadId, ended);
	if (!selection.turn && !selection.messageIds.length) return;
	inFlight = true;
	try {
		const rows = await read(threadId, selection);
		// A read that returns after the thread on screen changed is dropped.
		if (get(activeThreadId) !== threadId) return;
		if (rows.length) messages.update((current) => mergeFetchedHistoryRows(current, hydrateFetchedMessages(rows as never)));
	} catch (error) {
		console.error('[message cards] refresh failed', threadId, error);
	} finally {
		inFlight = false;
		if (trailing) {
			trailing = false;
			void refresh();
		}
	}
}

function schedule(): void {
	if (inFlight) {
		trailing = true;
		return;
	}
	if (pending) clearTimeout(pending);
	pending = setTimeout(() => {
		pending = null;
		void refresh();
	}, DEBOUNCE_MS);
}

/** The pending re-read outlives the view that armed it, so a test cancels it before the next test's reads. */
export function _cancelPendingMessageCardRefreshForTests(): void {
	if (pending) clearTimeout(pending);
	pending = null;
	turnEnded = false;
	trailing = false;
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
