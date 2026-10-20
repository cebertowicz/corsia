import type { ActiveTool } from '$lib/stores/threads';
import type { SubagentRef } from '$lib/stores/subagents';
import { rolledUpRoot, type ThreadTurnState } from '$lib/ws/turnState';
import { threadLiveStep, type LiveStep } from './plans/planLiveStep';

/** The one live presentation for a Focus work block, including spawned work. */
export type WorkBlockLive = {
	active: boolean;
	/** The visible thread itself has a running turn, so its spinner owns the accent color. */
	ownRunning: boolean;
	/** The visible thread's running turn wins; otherwise this is the newest active descendant's activity. */
	step: LiveStep | null;
	/** Null for the visible thread or until the subscribed child has delivered its title. */
	actorName: string | null;
	/** The chosen preview is a descendant's, even if its title has not arrived yet. */
	previewFromDescendant: boolean;
	/** Active descendants to subscribe for their existing title and tool snapshots. */
	descendantIds: readonly string[];
};

/**
 * Finds active descendant turns through `rolledUpRoot`, the predicate the
 * sidebar's `descendantRunning` also uses. `startedAt` is the only durable
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
		.filter(([id, state]) => rolledUpRoot(id, state) === threadId)
		.sort(([, left], [, right]) => (right.startedAt ?? -Infinity) - (left.startedAt ?? -Infinity));
	const descendantIds = descendants.map(([id]) => id);
	const child = descendants
		.map(([id]) => threadLiveStep(id, turns, toolsByThread))
		.find((step): step is LiveStep => step !== null) ?? null;
	// A running parent speaks for itself, even between tools (a step_text frame
	// empties its tools mid-turn); the worker's action shows only once it is idle.
	const step = own ?? child;
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
