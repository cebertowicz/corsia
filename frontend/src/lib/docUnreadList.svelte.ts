import { untrack } from 'svelte';
import { hasUnread, type DocComment } from '$lib/stores/docCommentProjection';

/** Keep unread cards in the open list until the next list session. */
export function retainUnreadList(isOpen: () => boolean, comments: () => DocComment[]) {
	let listed = $state(new Set<string>());
	$effect.pre(() => {
		if (!isOpen()) { listed = new Set(); return; }
		const waiting = comments().filter(hasUnread).map((comment) => comment.id);
		listed = new Set([...untrack(() => listed), ...waiting]);
	});
	return { has: (comment: DocComment) => hasUnread(comment) || listed.has(comment.id) };
}
