import type { Thread } from '$lib/stores/threads';
import type { ThreadPlanStateEntry } from '$lib/api/plans';

export type AgentActivity = Record<string, 'active' | 'done'>;
export type SidebarStates = Record<string, ThreadPlanStateEntry>;

export interface AgentRowSelection {
	visible: Thread[];
	hidden: Thread[];
	hiddenUnread: number;
	hiddenWorking: number;
}

export const COLLAPSED_AGENT_LIMIT = 5;

/** Slot-3 precedence, independent of slot-2 working motion. */
export function sidebarPriority(entry: ThreadPlanStateEntry | undefined): number {
	if (entry?.state === 'needs_input' || entry?.state === 'blocked_on_you') return 0;
	if (entry?.state === 'failed') return 1;
	return entry?.has_update ? 3 : 4;
}

/** Preserve caller order for equal slot-3 priority. */
export function sortThreadsBySidebarPriority(threads: Thread[], states: SidebarStates): Thread[] {
	return threads
		.map((thread, index) => ({ thread, index }))
		.sort((a, b) => sidebarPriority(states[a.thread.id]) - sidebarPriority(states[b.thread.id]) || a.index - b.index)
		.map(({ thread }) => thread);
}

/**
 * Skip the sort when thread membership, caller order, and plan priority are unchanged.
 * The cached order is a permutation of input indices, so rows always carry the current
 * thread objects; the previous array is returned only when every row is the same object.
 */
export function createSidebarSortMemo(): (threads: Thread[], states: SidebarStates) => Thread[] {
	let key = '';
	let order: number[] = [];
	let ordered: Thread[] = [];
	return (threads, states) => {
		const nextKey = threads.map((thread) => `${thread.id}:${sidebarPriority(states[thread.id])}`).join('|');
		if (nextKey !== key) {
			const index = new Map(threads.map((thread, i) => [thread, i]));
			key = nextKey;
			order = sortThreadsBySidebarPriority(threads, states).map((thread) => index.get(thread) as number);
		}
		const next = order.map((i) => threads[i]);
		if (next.length === ordered.length && next.every((thread, i) => thread === ordered[i])) return ordered;
		ordered = next;
		return ordered;
	};
}

/** Select a bounded Agent drawer by slot-3 priority while retaining ties and the selected row. */
export function selectAgentRows(
	agents: Thread[],
	activity: AgentActivity,
	states: SidebarStates,
	activeThreadId: string | null,
	expanded: boolean,
	limit = COLLAPSED_AGENT_LIMIT,
): AgentRowSelection {
	const alphabetical = [...agents].sort((a, b) => a.title.localeCompare(b.title));
	const ordered = sortThreadsBySidebarPriority(alphabetical, states);
	if (expanded || ordered.length <= limit) {
		return { visible: ordered, hidden: [], hiddenUnread: 0, hiddenWorking: 0 };
	}

	const selected = ordered.filter((agent) => agent.id === activeThreadId);
	const chosen = new Set(
		[...selected, ...ordered.filter((agent) => agent.id !== activeThreadId)]
			.slice(0, limit)
			.map((agent) => agent.id),
	);
	const visible = ordered.filter((agent) => chosen.has(agent.id));
	const hidden = ordered.filter((agent) => !chosen.has(agent.id));

	return {
		visible,
		hidden,
		hiddenUnread: hidden.filter((agent) => states[agent.id]?.has_update).length,
		hiddenWorking: hidden.filter((agent) => activity[agent.id] === 'active').length,
	};
}
