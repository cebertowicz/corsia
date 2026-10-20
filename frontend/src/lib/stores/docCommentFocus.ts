import { get, writable } from 'svelte/store';
import { docComments } from './docComments';

export const focusedDocComment = writable<string | null>(null);

// This module owns comment focus. Its writers are explicit UI picks and this open-to-resolved transition: a resolved
// comment shows its highlight only while focused, so resolving it from its popover, another canvas or an agent ends that
// focus and its highlight. A resolved comment the person picks in the drawer was never open here.
let open = new Set<string>();
docComments.subscribe((all) => {
	const rows = Object.values(all).flat();
	const id = get(focusedDocComment);
	if (id && open.has(id) && rows.some((row) => row.id === id && row.resolved && !row.region)) focusedDocComment.set(null);
	open = new Set(rows.filter((row) => !row.resolved).map((row) => row.id));
});
