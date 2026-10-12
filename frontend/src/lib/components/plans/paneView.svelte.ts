/**
 * One pane view of a thread's plan (`plans.pane_views`): which finished rows
 * are new since the user last looked, when they fold, and the report that
 * tells the server the user has seen them.
 *
 * The server lists the folded rows that settled after the user's last look
 * (`seen_through`); every listed folded row this view has not yet shown fold
 * is new and held open (`foldMotion.heldIds`). Once the pane shows a plan
 * loaded after it opened, in a visible page with user input in the last
 * `IDLE_MS`, the held rows sit for `PAUSE_MS`; then the view releases them, so
 * they fold, and reports the plan's `as_of` to the server. A hidden page, or
 * an open pane nobody is at, waits for the user. A row that finishes while the
 * pane is open is not released, so it gets the same pause, its own fold and
 * its own report.
 */
import { get } from 'svelte/store';
import type { OperatorEnvelope } from '$lib/api/plans';
import { recordThreadPlanSeen } from '$lib/api/plans';
import { token } from '$lib/stores/auth';
import { lastUserInputAt, onUserInput } from '$lib/userInput';
import { PAUSE_MS } from './foldWave';
import { heldIds, type PaneView } from './foldMotion.svelte';

/**
 * The rows this page has shown fold, per thread, until the server's mark says
 * so too. The next view reads them, so reopening the pane before the refreshed
 * read lands does not replay what this page already showed.
 */
const released = new Map<string, ReadonlySet<string>>();

/** How long after the user's last input an open pane still counts as looked at. */
export const IDLE_MS = 2 * 60_000;

function pageVisible(): boolean {
	return typeof document === 'undefined' || document.visibilityState === 'visible';
}

export interface PaneViewInput {
	threadId: () => string;
	payload: () => OperatorEnvelope | null;
	/** When the plan query last loaded or revalidated; null before its first load. */
	lastFreshAt: () => number | null;
}

export function createPaneView(input: PaneViewInput) {
	let threadId = $state(input.threadId());
	let openedAt = $state(Date.now());
	/** The rows this view has folded, whether or not the server heard of it. */
	let viewReleased = $state.raw<ReadonlySet<string>>(new Set());
	let visible = $state(pageVisible());
	$effect(() => {
		const update = () => { visible = pageVisible(); };
		document.addEventListener('visibilitychange', update);
		return () => document.removeEventListener('visibilitychange', update);
	});
	/** Input within `IDLE_MS`; the timer re-reads the clock, so later input extends it. */
	let recent = $state(false);
	$effect(() => {
		let timer: ReturnType<typeof setTimeout> | undefined;
		const check = () => {
			clearTimeout(timer);
			timer = undefined;
			const left = lastUserInputAt() + IDLE_MS - Date.now();
			recent = left > 0;
			if (recent) timer = setTimeout(check, left);
		};
		check();
		const off = onUserInput(() => { if (!recent) check(); });
		return () => { off(); clearTimeout(timer); };
	});

	// A different thread in the same pane is a new view.
	$effect.pre(() => {
		const next = input.threadId();
		if (next === threadId) return;
		threadId = next;
		openedAt = Date.now();
		viewReleased = new Set();
	});

	const view = $derived<PaneView>({ released: new Set([...(released.get(threadId) ?? []), ...viewReleased]) });
	const loaded = $derived((input.lastFreshAt() ?? -Infinity) >= openedAt && input.payload() !== null);

	/** The held rows while they may fold; a new arrival changes it and restarts the pause. */
	const due = $derived.by(() => {
		if (!loaded || !visible || !recent) return null;
		const held = heldIds(input.payload()?.plans[0]?.nodes ?? [], view);
		return held.size === 0 ? null : [...held].sort().join(' ');
	});

	function fold(): void {
		const payload = input.payload();
		if (payload === null) return;
		const thread = threadId;
		const mark = payload.as_of;
		const shown = heldIds(payload.plans[0]?.nodes ?? [], view);
		const previous = released.get(thread);
		const now = new Set([...(previous ?? []), ...shown]);
		viewReleased = new Set([...viewReleased, ...shown]);
		released.set(thread, now);
		const bearer = get(token);
		if (!bearer) return;
		void recordThreadPlanSeen(bearer, thread, mark).then((landed) => {
			// A report that did not land leaves the server's mark for the next view.
			if (landed || released.get(thread) !== now) return;
			if (previous) released.set(thread, previous); else released.delete(thread);
		});
	}

	$effect(() => {
		if (due === null) return;
		const timer = setTimeout(fold, PAUSE_MS);
		return () => clearTimeout(timer);
	});

	return {
		get view() { return view; },
		/** The tree remounts per view so its motion state never carries over. */
		get key() { return `${threadId}:${openedAt}`; },
	};
}
