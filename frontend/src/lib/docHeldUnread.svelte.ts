import { untrack } from 'svelte';
import type { CommentSurfaceState } from './docCommentSurfaceState.svelte';

type Held = 'comment' | 'reply';
type Listed = { id: string; newComment?: boolean; newReplyIds?: string[] };
type Range = { id: string; unread?: Held };

/**
 * The threads whose card is open: the thread a card, popover or sheet shows, or the card picked in the list or margin column.
 * The list or column being up opens nothing on its own.
 */
export function openCommentIds(state: CommentSurfaceState, focused: string | null): string[] {
	if (state?.kind === 'thread') return state.ids ?? [state.id];
	return state && focused ? [focused] : [];
}

/**
 * Opening a thread's card reads it at once; its highlight keeps the unread look until that card is no longer open, so the
 * sign the reader came to see stays under them. Only a thread that is unread while its card is open is held; every other
 * highlight shows the live state. The header count stays the real one. Call it from a component's setup.
 */
export function createHeldUnread(openIds: () => readonly string[], comments: () => Listed[]) {
	let held = $state(new Map<string, Held>());
	$effect.pre(() => {
		const open = new Set(openIds());
		const before = untrack(() => held);
		const next = new Map([...before].filter(([id]) => open.has(id)));
		for (const comment of comments()) {
			if (!open.has(comment.id) || next.has(comment.id)) continue;
			const kind = comment.newReplyIds?.length ? 'reply' : comment.newComment ? 'comment' : undefined;
			if (kind) next.set(comment.id, kind);
		}
		if (next.size !== before.size || [...next.keys()].some((id) => !before.has(id))) held = next;
	});
	return { paint: <T extends Range>(range: T): T => (range.unread || !held.has(range.id) ? range : { ...range, unread: held.get(range.id) }) };
}
