/**
 * Spacing for Activity-key reads.
 *
 * An Activity key matches every workspace plan write and its read rebuilds a
 * whole scope, so two request starts for one Activity key are at least
 * `ACTIVITY_MIN_GAP_MS` apart. A refresh asked for inside the gap joins one
 * trailing refresh that runs when the gap ends and reads the server's state
 * then: the latest state wins and the last change is never dropped. The first
 * read of a key, and any read after an auth change, is immediate. Operator
 * keys are not spaced. `plans.svelte.ts` owns the requests; this owns only
 * when one may start.
 */
import { isActivityKey, planKeyString, type PlanKey } from './planKeys';

/** The least time between two request starts for one Activity key. */
export const ACTIVITY_MIN_GAP_MS = 2000;

interface Spacing {
	lastStartedAt: number;
	trailing: { promise: Promise<void>; cancel: () => void } | null;
}

const spacings = new Map<string, Spacing>();

function spacingFor(keyStr: string): Spacing {
	let spacing = spacings.get(keyStr);
	if (!spacing) {
		spacing = { lastStartedAt: 0, trailing: null };
		spacings.set(keyStr, spacing);
	}
	return spacing;
}

/** Milliseconds until this key may start a request; 0 when it may start now. */
export function spacingWaitMs(key: PlanKey): number {
	if (!isActivityKey(key)) return 0;
	return Math.max(0, spacingFor(planKeyString(key)).lastStartedAt + ACTIVITY_MIN_GAP_MS - Date.now());
}

export function noteStarted(key: PlanKey): void {
	if (isActivityKey(key)) spacingFor(planKeyString(key)).lastStartedAt = Date.now();
}

/** Joins (or starts) the one refresh that runs when the key's gap ends. */
export function deferRefresh(key: PlanKey, waitMs: number, run: () => Promise<void>): Promise<void> {
	const spacing = spacingFor(planKeyString(key));
	if (spacing.trailing) return spacing.trailing.promise;
	let cancel = () => {};
	const promise = new Promise<void>((resolve) => {
		const timer = setTimeout(() => {
			spacing.trailing = null;
			resolve(run());
		}, waitMs);
		cancel = () => {
			clearTimeout(timer);
			spacing.trailing = null;
			resolve();
		};
	});
	spacing.trailing = { promise, cancel };
	return promise;
}

/** Drops a key's waiting trailing refresh, e.g. once nothing observes the key. */
export function cancelDeferred(keyStr: string): void {
	spacings.get(keyStr)?.trailing?.cancel();
}

/** Forgets one key (it is retired) or every key (the auth epoch changed). */
export function forgetSpacing(keyStr?: string): void {
	for (const [name, spacing] of spacings) {
		if (keyStr !== undefined && name !== keyStr) continue;
		spacing.trailing?.cancel();
		spacings.delete(name);
	}
}
