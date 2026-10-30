/**
 * How long a plan item's running work has been open, and the words that say so.
 * The count comes from the server's open time (`$lib/api/openTime`); the row
 * only asks for it and phrases it.
 */
import { readable } from 'svelte/store';
import type { OperatorNode } from '$lib/api/plans';
import type { OpenTimeQuery } from '$lib/api/openTime';
import { parseWsTimestamp } from '$lib/time';
import { UNKNOWN_STEP, type LiveStep } from './planLiveStep';

/**
 * The server's open-time question for a running step: from the step's first try to now, on the thread that
 * executes it (its subagents' Turns and outside waits are part of the answer). Null when no start is proven.
 */
export function stepOpenQuery(node: OperatorNode, fallbackThreadId: string): OpenTimeQuery | null {
	const starts = node.execution.attempts.map((attempt) => parseWsTimestamp(attempt.selected_at)).filter(Number.isFinite);
	if (starts.length === 0) return null;
	const threadId = node.execution.selection?.executor_thread_id ?? node.executor.thread_id ?? fallbackThreadId;
	const since = Math.min(...starts);
	return { key: `step|${node.id}|${threadId}|${since}`, threadId, since, until: null };
}

/** Whole minutes: the subline counts work, it is not a stopwatch. */
export function elapsedLabel(ms: number): string {
	const minutes = Math.floor(Math.max(0, ms) / 60_000);
	if (minutes < 1) return 'under 1 min';
	if (minutes < 60) return `${minutes} min`;
	const hours = Math.floor(minutes / 60);
	const rest = minutes % 60;
	return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

/** `Working 3 min · Reading plan`: the step's open time, or only its phrase while the server has not answered. */
export function liveStepLabel(step: LiveStep, openMs: number | null): string {
	if (openMs === null) return step.phrase;
	const elapsed = elapsedLabel(openMs);
	return step.phrase === UNKNOWN_STEP ? `Working ${elapsed}` : `Working ${elapsed} · ${step.phrase}`;
}

const TICK_MS = 15_000;

/** One shared clock for every live row, running only while a row shows. */
export const liveStepClock = readable(Date.now(), (set) => {
	set(Date.now());
	const timer = setInterval(() => set(Date.now()), TICK_MS);
	return () => clearInterval(timer);
});
