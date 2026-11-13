import { untrack } from 'svelte';

type Held = 'comment' | 'reply';
type Listed = { id: string; newComment?: boolean; newReplyIds?: string[] };
type Range = { id: string; unread?: Held };

/**
 * Opening a card or the list reads its threads at once; their highlights keep the unread look until that surface closes,
 * so the sign the reader came to see stays. The header count stays the real one. Call it from a component's setup.
 */
export function createHeldUnread(open: () => boolean, comments: () => Listed[]) {
	let held = $state(new Map<string, Held>());
	$effect.pre(() => {
		if (!open()) { if (untrack(() => held.size)) held = new Map(); return; }
		const next = new Map(untrack(() => held));
		for (const comment of comments()) {
			const kind = comment.newReplyIds?.length ? 'reply' : comment.newComment ? 'comment' : undefined;
			if (kind && !next.has(comment.id)) next.set(comment.id, kind);
		}
		if (next.size !== untrack(() => held.size)) held = next;
	});
	return { paint: <T extends Range>(range: T): T => (range.unread || !held.has(range.id) ? range : { ...range, unread: held.get(range.id) }) };
}
