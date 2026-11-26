import { COMMENT_POST_TIMEOUT_MS } from '$lib/api/boundedRequest';

/**
 * Background probes may hold at most this many of the browser's connections at
 * once. Safari over HTTP/1.1 opens six per origin, and a request the person
 * just made (a comment post) waits in the browser while all six are held. A
 * foreground sweep sends one probe per open document tab, so without a limit
 * it can hold all six. Probes the person asked for (a link press, a draft
 * load, an image view) call fetchRevision directly, outside this limit, and
 * never queue behind a sweep.
 */
const MAX_BACKGROUND_PROBES = 2;
/**
 * A probe holds its slot at most as long as a comment post may take, since both
 * wait in the same browser queue. One that times out frees its slot and counts
 * as a failed probe, so an unanswered request cannot stop the other checks.
 */
export const PROBE_TIMEOUT_MS = COMMENT_POST_TIMEOUT_MS;

interface SlotWaiter {
	visible: () => boolean;
	grant: () => void;
	cancel: (reason: ProbeCancelled) => void;
}

/** A waiting probe the watcher's stop withdrew; not a failure. */
export class ProbeCancelled extends Error {}

/** Slots of one watcher run; stop replaces it, so a late release never touches the next run's count. */
export interface ProbeLimiter {
	inFlight: number;
	waiters: SlotWaiter[];
}

let limiter: ProbeLimiter = { inFlight: 0, waiters: [] };

/** Takes a slot now (`wait` null) or returns the wait for one, with the limiter that owns it. */
export function takeProbeSlot(visible: () => boolean): { owner: ProbeLimiter; wait: Promise<void> | null } {
	const owner = limiter;
	if (owner.inFlight < MAX_BACKGROUND_PROBES) {
		owner.inFlight += 1;
		return { owner, wait: null };
	}
	return { owner, wait: new Promise((grant, cancel) => owner.waiters.push({ visible, grant, cancel })) };
}

/** Hands the slot to the visible tab's waiting probe first, otherwise the oldest. */
export function releaseProbeSlot(owner: ProbeLimiter): void {
	const visibleIndex = owner.waiters.findIndex((waiter) => waiter.visible());
	const [next] = owner.waiters.splice(visibleIndex >= 0 ? visibleIndex : 0, 1);
	if (next) next.grant();
	else owner.inFlight -= 1;
}

/** Starts the next watcher run with every slot free and withdraws the probes still waiting. */
export function resetProbeSlots(): void {
	const old = limiter;
	limiter = { inFlight: 0, waiters: [] };
	for (const waiter of old.waiters.splice(0)) waiter.cancel(new ProbeCancelled());
}
