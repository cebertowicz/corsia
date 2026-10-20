import type { ActiveTool } from '$lib/stores/threads';
import type { SubagentRef } from '$lib/stores/subagents';
import type { ThreadTurnState } from '$lib/ws/turnState';
import { threadLiveStep, type LiveStep } from './plans/planLiveStep';

/** The one live presentation for a Focus work block, including spawned work. */
export type WorkBlockLive = {
	active: boolean;
	/** The visible thread itself has a running turn, so its spinner owns the accent color. */
	ownRunning: boolean;
	/** Parent tool activity wins; otherwise this is the newest active descendant's activity. */
	step: LiveStep | null;
	/** Null for the visible thread or until the subscribed child has delivered its title. */
	actorName: string | null;
	/** The chosen preview is a descendant's, even if its title has not arrived yet. */
	previewFromDescendant: boolean;
	/** Active descendants to subscribe for their existing title and tool snapshots. */
	descendantIds: readonly string[];
};

/**
 * Reuses the server's visible-root lineage (the same truth `descendantRunning`
 * reads) to find active descendant turns. `startedAt` is the only durable
 * ordering field carried by the client turn state, so it decides "most recent".
 */
export function workBlockLive(
	threadId: string,
	turns: ReadonlyMap<string, ThreadTurnState>,
	toolsByThread: ReadonlyMap<string, ActiveTool[]>,
	subagents: ReadonlyMap<string, SubagentRef>,
): WorkBlockLive {
	const own = threadLiveStep(threadId, turns, toolsByThread);
	const descendants = [...turns.entries()]
		.filter(([id, state]) => id !== threadId && state.phase === 'active' && state.visibleRootThreadId === threadId)
		.sort(([, left], [, right]) => (right.startedAt ?? -Infinity) - (left.startedAt ?? -Infinity));
	const descendantIds = descendants.map(([id]) => id);
	const child = descendants
		.map(([id]) => threadLiveStep(id, turns, toolsByThread))
		.find((step): step is LiveStep => step !== null) ?? null;
	// A parent with a current tool speaks for itself. Before it has one, showing
	// the worker's current action makes the otherwise opaque wait legible.
	const step = own && !own.unknown ? own : child ?? own;
	const actorName = step && step.threadId !== threadId
		? subagents.get(step.threadId)?.name?.trim() || null
		: null;
	return {
		active: own !== null || child !== null,
		ownRunning: own !== null,
		step,
		actorName,
		previewFromDescendant: step?.threadId !== threadId,
		descendantIds,
	};
}
