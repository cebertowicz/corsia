import type { OperatorNode } from '$lib/api/plans';
import { parseWsTimestamp } from '$lib/time';

/**
 * Who gathers into a level's "Scheduled" row. The server decides what is quietly
 * scheduled, once, in the roll-up that reads every parent (`scheduled_until`: the
 * soonest end among the waits its agent set aside, set only when all of the node's
 * open work is such and nothing needs a person). The client derives no state: it
 * compares that one field with the clock.
 */

/** A scheduled wait must end further out than this to be tucked into the group. */
export const SCHEDULE_HORIZON_MS = 24 * 3_600_000;

const MINUTE_MS = 60_000;

/** When the node's scheduled work next comes due (epoch ms), or null when it is not scheduled. */
export function scheduledAtMs(node: OperatorNode): number | null {
	const ms = parseWsTimestamp(node.scheduled_until);
	return Number.isFinite(ms) ? ms : null;
}

/** Whether the node gathers into its level's Scheduled row at `now` (epoch ms): due more than a day out. */
export function isScheduledMember(node: OperatorNode, now: number): boolean {
	const at = scheduledAtMs(node);
	return at !== null && at - now > SCHEDULE_HORIZON_MS;
}

export interface ScheduledCluster {
	/** The shared moment (epoch ms of its first member's end). */
	at: number;
	members: OperatorNode[];
}

const minuteOf = (node: OperatorNode): number => Math.floor((scheduledAtMs(node) ?? 0) / MINUTE_MS);

/**
 * Members soonest first, those ending in the same local minute together in one
 * cluster; ties keep the order given (plan order).
 */
export function scheduledClusters(members: OperatorNode[]): ScheduledCluster[] {
	const sorted = [...members].sort((a, b) => minuteOf(a) - minuteOf(b));
	const clusters: ScheduledCluster[] = [];
	for (const member of sorted) {
		const last = clusters[clusters.length - 1];
		if (last && minuteOf(last.members[0]) === minuteOf(member)) last.members.push(member);
		else clusters.push({ at: scheduledAtMs(member) ?? 0, members: [member] });
	}
	return clusters;
}
