import { untrack } from 'svelte';
import type { DocComment } from '$lib/stores/docComments';
import type { ScrollOrigin } from '$lib/cm/comment-marks';
import { canvasCommentScrollTarget, canvasFocusedCommentScrollTarget, type CanvasScrollTarget } from '$lib/docCanvasComments';

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
	// A comment opened where the pointer clicked it stays where it is: its focus builds no scroll target.
	let stayOn: string | null = null;
	$effect(() => {
		const focused = source.focused();
		if (!focused) { target = null; stayOn = null; return; }
		untrack(() => {
			if (stayOn === focused) { target = null; return; }
			target = canvasFocusedCommentScrollTarget(target, source.comments(), source.text(), focused, source.listOpen());
		});
	});
	return {
		get value() { return target; },
		set value(next: CanvasScrollTarget | null) { stayOn = null; target = next; },
		/** Focus this comment without scrolling to it. */
		stay(id: string) { stayOn = id; target = null; },
		/** Scrolls to a comment's text as it is now placed; a comment with no text to jump to clears the target. */
		toComment(id: string, focus: boolean, origin: ScrollOrigin) { stayOn = null; target = canvasCommentScrollTarget(source.comments(), source.text(), id, focus, origin); },
	};
}
