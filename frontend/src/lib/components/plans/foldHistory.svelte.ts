/**
 * The folded history a plan tree has paged in, per level (`GET /api/plans/{id}/folded`).
 * A pane read lists only the open work and a recent window of folded rows; every
 * older finished row of a level is fetched here, 50 at a time, newest first, when
 * the user asks for it. Owns only what was fetched and each level's fetch state:
 * the server counts decide how many rows a level has (`planFoldLevels`).
 */
import { untrack } from 'svelte';
import { get } from 'svelte/store';
import {
	FOLDED_ROOT, readFoldedPage, readFoldedPath, type FoldedItem, type FoldedPath,
} from '$lib/api/plansFolded';
import { token } from '$lib/stores/auth';
import { ROOT_LEVEL } from './planTreeFold';

/** `idle`: more pages may follow; `end`: the last page arrived (or the level held nothing). */
export type HistoryStatus = 'idle' | 'loading' | 'error' | 'end';

export interface LevelHistory {
	items: FoldedItem[];
	status: HistoryStatus;
	/** The cursor for the next page; null before the first fetch and after the last. */
	cursor: string | null;
}

export const NO_HISTORY: LevelHistory = { items: [], status: 'idle', cursor: null };

export interface FoldHistoryInput {
	planId: () => string;
	/** The node whose children are this tree's top level when a sole wrapper was dropped; else null. */
	rootParent: () => string | null;
}

export function createFoldHistory(input: FoldHistoryInput) {
	let levels = $state.raw<Record<string, LevelHistory>>({});
	let epoch = 0;
	let inFlight = new Map<string, Promise<void>>();

	// Another plan is another history; a read still in flight for the old one is dropped.
	$effect.pre(() => {
		void input.planId();
		untrack(() => {
			epoch += 1;
			levels = {};
			inFlight = new Map();
		});
	});

	const at = (level: string): LevelHistory => levels[level] ?? NO_HISTORY;
	const put = (level: string, next: LevelHistory) => { levels = { ...levels, [level]: next }; };
	const parentOf = (level: string) => (level === ROOT_LEVEL ? input.rootParent() ?? FOLDED_ROOT : level);

	/** Fetch the next page of a level; while one is in flight, that fetch; a no-op after the last. */
	function loadMore(level: string): Promise<void> {
		const pending = inFlight.get(level);
		if (pending) return pending;
		if (at(level).status === 'end') return Promise.resolve();
		const fetching = fetchPage(level).finally(() => {
			if (inFlight.get(level) === fetching) inFlight.delete(level);
		});
		inFlight.set(level, fetching);
		return fetching;
	}

	async function fetchPage(level: string): Promise<void> {
		const before = at(level);
		const run = epoch;
		const planId = input.planId();
		put(level, { ...before, status: 'loading' });
		const bearer = get(token);
		const outcome = bearer
			? await readFoldedPage(bearer, planId, parentOf(level), before.cursor)
			: ({ status: 'unauthorized' } as const);
		if (run !== epoch) return;
		if (outcome.status !== 'fresh') {
			put(level, { ...before, status: 'error' });
			return;
		}
		const known = new Set(before.items.map((item) => item.id));
		const items = [...before.items, ...outcome.payload.items.filter((item) => !known.has(item.id))];
		const cursor = outcome.payload.next_cursor;
		put(level, { items, cursor, status: cursor === null ? 'end' : 'idle' });
	}

	/** Page a level until it holds `id`, or its history ends or fails; whether it holds it. */
	async function loadUntil(level: string, id: string): Promise<boolean> {
		while (!at(level).items.some((item) => item.id === id)) {
			const status = at(level).status;
			if (status === 'end' || status === 'error') return false;
			await loadMore(level);
			if (at(level).status === 'error') return false;
		}
		return true;
	}

	/** A node's ancestors and place among its level's folded rows; null when the read did not succeed. */
	async function locate(nodeId: string): Promise<FoldedPath | null> {
		const bearer = get(token);
		if (!bearer) return null;
		const outcome = await readFoldedPath(bearer, input.planId(), nodeId);
		return outcome.status === 'fresh' ? outcome.payload : null;
	}

	/**
	 * Page in every unloaded level on the way to a node the read did not list; whether the node
	 * is now loaded. `loaded` says whether a row is already in the tree (listed or fetched).
	 */
	async function openPath(nodeId: string, loaded: (id: string) => boolean): Promise<boolean> {
		const path = await locate(nodeId);
		if (!path?.node) return false;
		const chain = [...path.ancestors.map((ancestor) => ancestor.id), nodeId];
		const root = input.rootParent();
		let level = ROOT_LEVEL;
		for (const id of chain.slice(root === null ? 0 : chain.indexOf(root) + 1)) {
			if (!loaded(id) && !(await loadUntil(level, id))) return false;
			level = id;
		}
		return true;
	}

	return {
		level: at,
		loadMore,
		loadUntil,
		openPath,
		/** Try a failed page again. */
		retry(level: string): Promise<void> {
			if (at(level).status === 'error') put(level, { ...at(level), status: 'idle' });
			return loadMore(level);
		},
		rootParent: input.rootParent,
	};
}

export type FoldHistory = ReturnType<typeof createFoldHistory>;
