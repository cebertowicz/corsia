/**
 * Activity's read of the one attention queue (`view=activity`). The server owns membership and order; this holds
 * the last read and re-reads whenever something that settles an item changes. It never removes an item locally:
 * an item leaves the list only because a re-read no longer returns it.
 *
 * At most one read is open at a time. A browser holds few connections to one server (six over HTTP/1.1,
 * which remote access uses), so a read left open while another starts takes a connection a comment post
 * needs: a new scope or a stop cancels the read in flight, and changes during a read coalesce into one more.
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
	/** Cancels the open read, so it gives its connection back. */
	let open: AbortController | null = null;

	function cancel(): void {
		open?.abort();
		open = null;
	}

	async function readOnce(): Promise<void> {
		const mine = generation;
		const scope = projectId();
		const read = new AbortController();
		open = read;
		try {
			const next = await fetchActivityQueue(scope, read.signal);
			if (mine !== generation) return;
			queue = next;
			failure = null;
			status = 'ready';
		} catch (error) {
			if (mine !== generation || read.signal.aborted) return;
			failure = error instanceof ActivityQueueError ? error.status : 0;
			// A failed re-read keeps the list the person was working; only a first read shows the failure.
			if (!queue) status = 'error';
		} finally {
			if (open === read) open = null;
		}
	}

	/** Re-reads the queue; reads asked for while one is running coalesce into one more. */
	function reload(): Promise<void> {
		if (inFlight) {
			again = true;
			return inFlight;
		}
		const mine = generation;
		const run: Promise<void> = (async () => {
			// A run a new scope replaced stops here: the replacing run owns `again` from then on.
			do {
				again = false;
				await readOnce();
			} while (again && mine === generation);
		})().finally(() => {
			if (inFlight === run) inFlight = null;
		});
		inFlight = run;
		return run;
	}

	/** Starts a fresh read for the current scope and keeps re-reading on change; returns the stop. */
	function start(): () => void {
		generation += 1;
		cancel();
		queue = null;
		status = 'loading';
		failure = null;
		inFlight = null;
		again = false;
		void reload();
		const offs = RELOAD_EVENTS.map((event) => on(event, () => void reload()));
		return () => {
			generation += 1;
			cancel();
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
