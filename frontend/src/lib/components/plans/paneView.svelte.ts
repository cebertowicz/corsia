/**
 * One pane view of a thread's plan (`plans.pane_views`): which finished rows
 * are new since the user last looked, when they fold, and the report that
 * tells the server the user has seen them.
 *
 * A folded row that settled after `seen_through` is new and held open
 * (`foldWave.heldRows`). Once the pane shows a plan loaded after it opened,
 * in a visible page with user input in the last `IDLE_MS`, the held rows sit
 * for `PAUSE_MS`; then the view advances its mark to that plan's `as_of`, so
 * they fold, and reports it. A hidden page, or an open pane nobody is at,
 * waits for the user. A row that finishes while the pane is open is new
 * against the advanced mark, so it gets the same pause, its own fold and its
 * own report.
 */
import { get } from 'svelte/store';
import type { OperatorEnvelope } from '$lib/api/plans';
import { recordThreadPlanSeen } from '$lib/api/plans';
import { token } from '$lib/stores/auth';
import { lastUserInputAt, onUserInput } from '$lib/userInput';
import { PAUSE_MS, heldRows } from './foldWave';
import type { PaneView } from './foldMotion.svelte';

/**
 * The mark this page last reported per thread. The next view floors the
 * server's mark with it, so reopening the pane before the refreshed read
 * lands does not replay what this page already showed.
 */
const reported = new Map<string, string>();

/** How long after the user's last input an open pane still counts as looked at. */
export const IDLE_MS = 2 * 60_000;

function later(a: string | null | undefined, b: string | null | undefined): string | null {
	if (a == null) return b ?? null;
	if (b == null) return a;
	return Date.parse(b) > Date.parse(a) ? b : a;
}

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
	/** The mark this view has advanced to by folding; null before its first fold. */
	let advanced = $state<string | null>(null);
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
		advanced = null;
	});

	const seenThrough = $derived(later(later(input.payload()?.seen_through, reported.get(threadId)), advanced));
	const loaded = $derived((input.lastFreshAt() ?? -Infinity) >= openedAt && input.payload() !== null);

	/** The held rows while they may fold; a new arrival changes it and restarts the pause. */
	const due = $derived.by(() => {
		if (!loaded || !visible || !recent) return null;
		const held = heldRows(input.payload()?.plans[0]?.nodes ?? [], seenThrough);
		return held.size === 0 ? null : [...held].sort().join(' ');
	});

	function fold(): void {
		const payload = input.payload();
		if (payload === null) return;
		const view = threadId;
		const mark = payload.as_of;
		advanced = mark;
		const previous = reported.get(view);
		reported.set(view, mark);
		const bearer = get(token);
		if (!bearer) return;
		void recordThreadPlanSeen(bearer, view, mark).then((landed) => {
			// A report that did not land leaves the server's mark for the next view.
			if (landed || reported.get(view) !== mark) return;
			if (previous) reported.set(view, previous); else reported.delete(view);
		});
	}

	$effect(() => {
		if (due === null) return;
		const timer = setTimeout(fold, PAUSE_MS);
		return () => clearTimeout(timer);
	});

	const view = $derived<PaneView>({ seenThrough });
	return {
		get view() { return view; },
		/** The tree remounts per view so its motion state never carries over. */
		get key() { return `${threadId}:${openedAt}`; },
	};
}
