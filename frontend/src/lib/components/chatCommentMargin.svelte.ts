import { observeCanvasWidth } from '$lib/docCommentDrawer';
import type { MarginComposer } from '$lib/docMarginSource';

/**
 * Whether the comment column fits beside a centred transcript without moving it: the free space on its right, half of
 * what the canvas leaves beside the transcript at its widest, holds the whole column. Unreadable sizes never fit.
 */
export const roomBeside = (canvasWidth: number, transcriptWidth: number, columnWidth: number) =>
	Number.isFinite(transcriptWidth) && Number.isFinite(columnWidth) && columnWidth > 0 && (canvasWidth - transcriptWidth) / 2 >= columnWidth;

/**
 * Whether a chat canvas shows the comment margin beside its transcript, and which card in that margin is picked. The
 * transcript and composer stay where they sit with no comments, so the column shows only while no artifact pane is open
 * beside the chat and the canvas has free space for it on the transcript's right (both widths are the app's tokens);
 * otherwise a highlight opens a popover.
 * The comments themselves stay in the message store, so a panel needs only these and the composer being written.
 */
export function createChatCommentMargin(canvas: () => HTMLElement | null, threadId: () => string | null, hasComments: () => boolean, paneOpen: () => boolean) {
	let fits = $state(false);
	/** The thread whose margin card is picked, shared between the transcript's highlights and the column. */
	let selected = $state<string | null>(null);
	/** A comment being written or edited on the transcript while the column is up: the column draws it as a card level with its text. */
	let composer = $state<MarginComposer | null>(null);
	$effect(() => {
		const el = canvas();
		return observeCanvasWidth(el, (width) => {
			const style = el ? getComputedStyle(el) : null;
			const size = (name: string) => parseFloat(style?.getPropertyValue(name) ?? '');
			fits = roomBeside(width, size('--transcript-max-width'), size('--margin-column'));
		});
	});
	const shown = $derived(!!threadId() && !paneOpen() && fits && hasComments());
	return {
		get shown() { return shown; },
		get selectedId() { return selected; },
		set selectedId(value: string | null) { selected = value; },
		get composer() { return composer; },
		set composer(value: MarginComposer | null) { composer = value; },
	};
}
