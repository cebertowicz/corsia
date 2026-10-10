import type { OperatorNode } from '$lib/api/plans';
import { isScheduledTask, waitEndsAtMs } from './planWaits';

/**
 * Who gathers into a level's "Scheduled" row: a leaf task its own agent set aside
 * until a date more than a day away, that needs no one. The pure rules behind
 * `arrangeItems`; membership is a function of the node and the clock only.
 */

/** A scheduled wait must end further out than this to be tucked into the group. */
export const SCHEDULE_HORIZON_MS = 24 * 3_600_000;

const MINUTE_MS = 60_000;

/** Whether the node carries an ask raised for the user. */
function hasRaisedAsk(node: OperatorNode): boolean {
	return node.artifacts.some((artifact) => artifact.role === 'attention' && artifact.attention_state === 'raised');
}

/**
 * Whether a task is quietly scheduled at `now` (epoch ms). A paused task or plan
 * is not waiting, so it has no timed wait and is never a member; a wait that
 * ends within the horizon, or has already ended, stays an ordinary row.
 */
export function isScheduledMember(node: OperatorNode, now: number): boolean {
	const ends = waitEndsAtMs(node);
	return node.kind === 'task' && isScheduledTask(node) && ends !== null && ends - now > SCHEDULE_HORIZON_MS
		&& !node.requires_user_action && node.held_by !== 'user' && node.assignee !== 'user' && !hasRaisedAsk(node);
}

/** The ids of nodes some other node names as its parent: everything else is a leaf. */
export function parentIdsOf(nodes: Iterable<OperatorNode>): Set<string> {
	const parents = new Set<string>();
	for (const node of nodes) if (node.parent_id) parents.add(node.parent_id);
	return parents;
}

export interface ScheduledCluster {
	/** The shared moment (epoch ms of its first member's end). */
	at: number;
	members: OperatorNode[];
}

const minuteOf = (node: OperatorNode): number => Math.floor((waitEndsAtMs(node) ?? 0) / MINUTE_MS);

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
		else clusters.push({ at: waitEndsAtMs(member) ?? 0, members: [member] });
	}
	return clusters;
}
