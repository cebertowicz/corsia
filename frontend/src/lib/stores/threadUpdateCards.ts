/**
 * Each open thread's plan update cards (AS9, AS17) and this visit's
 * "Since your last visit" divider (AS18).
 *
 * The newest bounded card window is read from the lowest loaded message
 * position (0 once the first page of history is in), and re-read when a `plans_changed` or
 * `thread_updates_changed` nudge names the thread. The divider is read once
 * per open from the server's pre-claim visit snapshot, and never re-read
 * during the visit. A failed opening read records a failed divider state so the
 * transcript can recover its live-edge behavior instead of waiting to land.
 */
import { get, writable } from 'svelte/store';
import { readUpdateCards, type UpdateCard, type VisitDivider } from '$lib/api/threadUpdates';
import { on } from '$lib/ws';
import { token } from './auth';
import type { PlansChangedMessage } from './planKeys';

export type ThreadCards = { fromSeq: number; cards: UpdateCard[] };
export type VisitDividerState = { divider: VisitDivider | null; failed: boolean };

export const threadUpdateCards = writable<Record<string, ThreadCards>>({});
/** The opening divider outcome for each currently open thread. */
export const visitDividers = writable<Record<string, VisitDividerState>>({});

const readSeq = new Map<string, number>();
/** Identifies the visit whose one opening divider read may still commit. */
const visitSeq = new Map<string, number>();

async function readWindow(bearer: string, threadId: string, fromSeq: number, divider: boolean) {
	return readUpdateCards(bearer, threadId, { fromSeq, divider });
}

/** Read a thread's cards from `fromSeq`; with `divider`, also freeze this visit's divider. */
export async function loadUpdateCards(
	threadId: string,
	fromSeq: number,
	options: { divider?: boolean } = {},
): Promise<void> {
	const bearer = get(token);
	if (!bearer) return;
	const mine = (readSeq.get(threadId) ?? 0) + 1;
	readSeq.set(threadId, mine);
	const visit = options.divider ? (visitSeq.get(threadId) ?? 0) + 1 : null;
	if (visit !== null) visitSeq.set(threadId, visit);
	const result = await readWindow(bearer, threadId, fromSeq, Boolean(options.divider));
	if (!result) {
		if (visit !== null && visitSeq.get(threadId) === visit) {
			visitDividers.update((all) => ({ ...all, [threadId]: { divider: null, failed: true } }));
			openAtDivider.update((open) => open === threadId ? null : open);
		}
		return;
	}
	if (visit !== null && visitSeq.get(threadId) === visit) {
		visitDividers.update((all) => ({ ...all, [threadId]: { divider: result.divider, failed: false } }));
	}
	if (readSeq.get(threadId) !== mine) return;
	threadUpdateCards.update((all) => ({ ...all, [threadId]: { fromSeq, cards: result.cards } }));
}

/** The visit ended: the next open reads a fresh divider. */
export function endVisit(threadId: string): void {
	// Invalidate an opening read that may still be in flight for the old visit.
	visitSeq.set(threadId, (visitSeq.get(threadId) ?? 0) + 1);
	readSeq.set(threadId, (readSeq.get(threadId) ?? 0) + 1);
	visitDividers.update((all) => {
		const next = { ...all };
		delete next[threadId];
		return next;
	});
	threadUpdateCards.update((all) => {
		const next = { ...all };
		delete next[threadId];
		return next;
	});
}

let watching = false;

/** Re-read loaded threads a nudge names; subscribes once per page. */
export function watchUpdateCardNudges(): void {
	if (watching) return;
	watching = true;
	const reload = (ids: string[]) => {
		const loaded = get(threadUpdateCards);
		for (const id of new Set(ids)) {
			if (loaded[id]) void loadUpdateCards(id, loaded[id].fromSeq);
		}
	};
	on('plans_changed', (message: PlansChangedMessage) => {
		reload(message.plans.filter((change) => change.owner.kind === 'thread').map((change) => change.owner.id));
	});
	// An ask card's title is the artifact's, so a rename or retirement re-reads every loaded thread's cards.
	on('artifacts_changed', () => reload(Object.keys(get(threadUpdateCards))));
	on('thread_updates_changed', (message: { thread_ids?: unknown }) => {
		const named = Array.isArray(message?.thread_ids) ? message.thread_ids : [];
		reload(named.filter((id): id is string => typeof id === 'string'));
	});
}

/** The thread the update dot just opened, to be shown at its divider once. */
export const openAtDivider = writable<string | null>(null);
