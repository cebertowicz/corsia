import { get, writable } from 'svelte/store';
import { docComments } from './docComments';

export const focusedDocComment = writable<string | null>(null);

// A resolved comment shows its highlight only while it is focused (the drawer focuses one to show where it sat), so
// resolving the focused comment, from its popover, another canvas or an agent, ends its focus and with it the highlight.
// Only the open-to-resolved change does: a resolved comment the person picks in the drawer was never open here.
let open = new Set<string>();
docComments.subscribe((all) => {
	const rows = Object.values(all).flat();
	const id = get(focusedDocComment);
	if (id && open.has(id) && rows.some((row) => row.id === id && row.resolved && !row.region)) focusedDocComment.set(null);
	open = new Set(rows.filter((row) => !row.resolved).map((row) => row.id));
});
