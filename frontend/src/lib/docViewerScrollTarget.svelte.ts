import { untrack } from 'svelte';
import type { DocComment } from '$lib/stores/docComments';
import { canvasFocusedCommentScrollTarget, type CanvasScrollTarget } from '$lib/docCanvasComments';

/**
 * Where the editor should scroll next: the focused comment's text, kept while that comment stays focused, and any target
 * the viewer sets itself (a jump from the activity strip, a comment just posted). Call during component init.
 */
export function createCommentScrollTarget(source: {
	focused: () => string | null;
	comments: () => DocComment[];
	text: () => string;
	listOpen: () => boolean;
}) {
	let target = $state<CanvasScrollTarget | null>(null);
	$effect(() => {
		const focused = source.focused();
		if (!focused) { target = null; return; }
		untrack(() => {
			target = canvasFocusedCommentScrollTarget(target, source.comments(), source.text(), focused, source.listOpen());
		});
	});
	return {
		get value() { return target; },
		set value(next: CanvasScrollTarget | null) { target = next; },
	};
}
