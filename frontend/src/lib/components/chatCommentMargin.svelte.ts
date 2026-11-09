import { hasRoomForMargin } from '$lib/docCommentSurfaceState.svelte';
import { observeCanvasWidth } from '$lib/docCommentDrawer';

/**
 * Whether a chat canvas shows the comment margin beside its transcript, and which card in that margin is
 * picked. The room rule is the document's own (`hasRoomForMargin`), measured on this canvas as the document
 * measures its own; the comments themselves stay in the message store, so a panel needs only these two.
 */
export function createChatCommentMargin(canvas: () => HTMLElement | null, threadId: () => string | null, hasComments: () => boolean) {
	let width = $state(0);
	/** The thread whose margin card is picked, shared between the transcript's highlights and the column. */
	let selected = $state<string | null>(null);
	$effect(() => observeCanvasWidth(canvas(), (value) => width = value));
	const shown = $derived(!!threadId() && hasRoomForMargin(width) && hasComments());
	return {
		get shown() { return shown; },
		get selectedId() { return selected; },
		set selectedId(value: string | null) { selected = value; },
	};
}
