import type { OperatorExecutionCause, OperatorNode } from '$lib/api/plans';
import { parseWsTimestamp } from '$lib/time';

/** What a task's timed wait says about itself: when it ends, whether the agent chose the date. */

/** The wait cause carrying a deadline (`ends_at`), or null for an event, dependency or review wait. */
export function timedWait(node: OperatorNode): OperatorExecutionCause | null {
	if (node.execution.condition !== 'waiting') return null;
	return node.execution.causes.find((item) => item.kind === 'waiting' && item.ends_at) ?? null;
}

/** When a wait ends on its own, in epoch ms; null for a wait with no deadline. */
export function waitEndsAtMs(node: OperatorNode): number | null {
	const ms = parseWsTimestamp(timedWait(node)?.ends_at);
	return Number.isFinite(ms) ? ms : null;
}

/** Whether the agent itself set this task aside until a date it chose. */
export function isScheduledTask(node: OperatorNode): boolean {
	return !!timedWait(node)?.scheduled_reason && waitEndsAtMs(node) !== null;
}

const HOUR_MS = 3_600_000;
const MINUTE_MS = 60_000;

/** "ends in 12 min" for a wait ending within the hour; null otherwise. */
export function waitCountdown(node: OperatorNode, now: Date = new Date()): string | null {
	const ends = waitEndsAtMs(node);
	if (ends === null) return null;
	const left = ends - now.getTime();
	if (left <= 0 || left > HOUR_MS) return null;
	const minutes = Math.round(left / MINUTE_MS);
	return minutes < 1 ? 'ends in under a minute' : `ends in ${minutes} min`;
}
