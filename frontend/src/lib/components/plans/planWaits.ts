import type { OperatorExecutionCause, OperatorNode } from '$lib/api/plans';
import { formatMoment, parseWsTimestamp } from '$lib/time';

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

/** Whether a watch's time has passed: the agent is being woken, so every surface reads "back now" (EW10). */
export function waitPassed(until: string | null | undefined, now: Date = new Date()): boolean {
	const ms = parseWsTimestamp(until);
	return Number.isFinite(ms) && ms <= now.getTime();
}

/** The short form every surface shows: "back 11:46 PM", "back Thursday 9:00 AM", or "back now" once passed (EW10); empty when the moment cannot be formatted. */
export function waitBack(until: string, now: Date = new Date()): string {
	if (waitPassed(until, now)) return 'back now';
	const at = formatMoment(new Date(parseWsTimestamp(until)), now, { compact: true, plain: true, clock: true });
	return at ? `back ${at}` : '';
}

/** The set-aside row's line: "Set aside until Thursday 9:00 AM", or "Back now" once the time has passed (EW7, EW10). */
export function setAsideLine(until: string, now: Date = new Date()): string {
	if (waitPassed(until, now)) return 'Back now';
	const at = formatMoment(new Date(parseWsTimestamp(until)), now, { compact: true, plain: true, clock: true });
	return at ? `Set aside until ${at}` : 'Set aside';
}

/** The long form for titles and accessible names: "checks back today at 11:46 PM", or "checks back now"; empty when the moment cannot be formatted. */
export function waitChecksBack(until: string, now: Date = new Date()): string {
	if (waitPassed(until, now)) return 'checks back now';
	const at = formatMoment(new Date(parseWsTimestamp(until)), now);
	return at ? `checks back ${at}` : '';
}

/** A plan wait the server names on this row: a watch with a deadline, apart from set-aside, dependency and worker waits (EW11). */
export function isOutsideWait(node: OperatorNode): boolean {
	return node.execution.condition === 'waiting' && !node.child && !!node.wait;
}

/**
 * The reason line of a row that waits outside the plan (EW11): "{label} · back {time}", from the server's wait.
 * Null for a delegated step whose child chip says it; undefined for every other row.
 */
export function outsideWaitReason(node: OperatorNode, now: Date): string | null | undefined {
	if (node.child?.wait && node.execution.condition === 'waiting') return null;
	if (!isOutsideWait(node) || !node.wait) return undefined;
	const more = node.wait.more > 0 ? ` +${node.wait.more} more` : '';
	return [`${node.wait.label}${more}`, waitBack(node.wait.until, now)].filter(Boolean).join(' · ');
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

/** When a timed wait ends, as a person says it: "tomorrow at 9:00 AM", or "ends in 12 min" inside the hour. */
function endMoment(node: OperatorNode, now: Date, clock = false): string | null {
	const ends = waitEndsAtMs(node);
	if (ends === null) return null;
	return waitCountdown(node, now) ?? formatMoment(new Date(ends), now, { clock });
}

/**
 * A task its agent scheduled, said as a person reads it: "tomorrow at 9:00 AM ·
 * why", or "ends in 12 min · why" inside the hour, from the wait's own
 * deadline, never the instant in the prose. Work that is running says only why,
 * because a running task is not waiting for the time.
 */
export function scheduledLine(node: OperatorNode, now: Date, running: boolean, clock = false): string | null {
	const why = timedWait(node)?.scheduled_reason;
	const when = endMoment(node, now, clock);
	if (!why || when === null) return null;
	return running ? why : `${when} · ${why}`;
}
