import { derived } from 'svelte/store';
import { activeThreadId } from './threads';
import { threadPlanStates } from './threadPlanStates';
import { rolledUpRoot, turnStates } from '../ws/turnState';

/**
 * Each thread's motion word: `active` while its own Turn runs, `done` when the
 * server says an update arrived since the user's last visit (the update dot,
 * AS7/AS10). Seen state is the server's alone, the same on every device; the
 * thread in front of the user never reads `done`.
 */
export const threadActivity = derived(
	[turnStates, threadPlanStates, activeThreadId],
	([$turnStates, $threadPlanStates, $activeThreadId]) => {
		const activity: Record<string, 'active' | 'done'> = {};
		for (const [threadId, entry] of Object.entries($threadPlanStates)) {
			if (entry.has_update && threadId !== $activeThreadId) activity[threadId] = 'done';
		}
		for (const [threadId, state] of $turnStates) {
			if (state.phase === 'active') activity[threadId] = 'active';
		}
		return activity;
	},
);

/**
 * Visible threads whose own Turn is idle while a descendant (spawned_by chain)
 * runs one. The server names each running Turn's visible root
 * (`visible_root_thread_id`); the mark stays gray and never claims the visible thread
 * itself is working. A Turn running on the visible thread's own row is the
 * row's blue spinner, not this set.
 */
export const descendantRunning = derived(turnStates, ($turnStates) => {
	const running = new Set<string>();
	for (const [threadId, state] of $turnStates) {
		const root = rolledUpRoot(threadId, state);
		if (root !== null) running.add(root);
	}
	return running;
});
