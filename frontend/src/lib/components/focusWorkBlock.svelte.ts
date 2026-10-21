import { fromStore } from 'svelte/store';
import { activeToolsByThread } from '$lib/stores/threads';
import { subagentMeta } from '$lib/stores/subagents';
import { watchThread } from '$lib/ws';
import { turnStates } from '$lib/ws/turnState';
import { workBlockLive, type WorkBlockLive } from './workBlockLive';

const IDLE: WorkBlockLive = { active: false, ownRunning: false, step: null, actorName: null, previewFromDescendant: false, descendantIds: [] };

/**
 * Focus's live work block for one transcript, descendants included. This adds
 * descendant visibility only to the work block; delivery still follows the
 * visible thread's own turn state. While Focus is on, it subscribes to each active
 * descendant's existing title and tool snapshots; `watchThread` shares the socket
 * subscription, released as soon as the descendant settles or the transcript unmounts.
 */
export function createFocusWorkBlock(
	threadId: () => string | null | undefined,
	focusMode: () => boolean,
): { readonly current: WorkBlockLive } {
	const turns = fromStore(turnStates);
	const tools = fromStore(activeToolsByThread);
	const subagents = fromStore(subagentMeta);
	const current = $derived.by(() => {
		const id = threadId();
		return id ? workBlockLive(id, turns.current, tools.current, subagents.current) : IDLE;
	});
	const watches = new Map<string, () => void>();
	$effect(() => {
		const wanted = new Set(focusMode() ? current.descendantIds : []);
		for (const [id, release] of watches) if (!wanted.has(id)) {
			release();
			watches.delete(id);
		}
		for (const id of wanted) if (!watches.has(id)) watches.set(id, watchThread(id));
	});
	$effect(() => () => {
		for (const release of watches.values()) release();
		watches.clear();
	});
	return { get current() { return current; } };
}
