/**
 * Activity's read of the one attention queue (`view=activity`). The server owns membership and order; this holds
 * the last read and re-reads whenever something that settles an item changes. It never removes an item locally:
 * an item leaves the list only because a re-read no longer returns it.
 */
import { on } from '$lib/ws/events';
import {
	fetchActivityQueue,
	ActivityQueueError,
	type ActivityQueue,
} from '$lib/api/activityQueue';

export type ActivityQueueStatus = 'loading' | 'ready' | 'error';

/** The events after which an item may have settled, moved or arrived. */
const RELOAD_EVENTS = [
	'plans_changed',
	'held_reply_changed',
	'artifacts_changed',
	'doc_comment_changed',
	'chat_comments_changed',
	'connected',
] as const;

export function createActivityQueue(projectId: () => string | null) {
	let status = $state<ActivityQueueStatus>('loading');
	let queue = $state<ActivityQueue | null>(null);
	/** HTTP status of the last failed read; 0 when the request never reached the server. */
	let failure = $state<number | null>(null);

	let inFlight: Promise<void> | null = null;
	let again = false;
	/** Bumped when the scope changes, so a read for the old scope never lands. */
	let generation = 0;

	async function readOnce(): Promise<void> {
		const mine = generation;
		const scope = projectId();
		try {
			const next = await fetchActivityQueue(scope);
			if (mine !== generation) return;
			queue = next;
			failure = null;
			status = 'ready';
		} catch (error) {
			if (mine !== generation) return;
			failure = error instanceof ActivityQueueError ? error.status : 0;
			// A failed re-read keeps the list the person was working; only a first read shows the failure.
			if (!queue) status = 'error';
		}
	}

	/** Re-reads the queue; reads asked for while one is running coalesce into one more. */
	function reload(): Promise<void> {
		if (inFlight) {
			again = true;
			return inFlight;
		}
		const run: Promise<void> = (async () => {
			do {
				again = false;
				await readOnce();
			} while (again);
		})().finally(() => {
			if (inFlight === run) inFlight = null;
		});
		inFlight = run;
		return run;
	}

	/** Starts a fresh read for the current scope and keeps re-reading on change; returns the stop. */
	function start(): () => void {
		generation += 1;
		queue = null;
		status = 'loading';
		failure = null;
		inFlight = null;
		again = false;
		void reload();
		const offs = RELOAD_EVENTS.map((event) => on(event, () => void reload()));
		return () => {
			generation += 1;
			for (const off of offs) off();
		};
	}

	return {
		get status() {
			return status;
		},
		get queue() {
			return queue;
		},
		get failure() {
			return failure;
		},
		reload,
		start,
	};
}

export type ActivityQueueStore = ReturnType<typeof createActivityQueue>;
