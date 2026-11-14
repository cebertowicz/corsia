/** Where a commented passage can be seen in the transcript: below the shell header, above the chat composer or a comment sheet. */
import { restingTop } from '$lib/cm/visible-region';
import { onSheetMoved } from '$lib/docCommentDrawer';
import { capSheetAboveText, giveBackRoom, placeAboveSheet } from '$lib/sheetClearance';

/** The part of the transcript a popover may cover: under the header and above the chat composer. */
export function transcriptBounds(root: HTMLElement | null): DOMRect | null {
	if (!root) return null;
	const box = root.getBoundingClientRect();
	const header = root.ownerDocument.querySelector('.shell-header')?.getBoundingClientRect();
	const composer = root.closest('.chat-content')?.querySelector('.input-floater')?.getBoundingClientRect();
	const top = Math.max(box.top, header?.bottom ?? box.top);
	const bottom = Math.min(box.bottom, composer?.top ?? box.bottom);
	return new DOMRect(box.left, top, box.width, Math.max(0, bottom - top));
}

/**
 * Keeps the passage in view while a sheet (the comment-writing sheet, or an opened comment's) is open over the transcript: the one owner of
 * that for chat. `capText`, when it returns the pressed text (an opened comment's sheet), first caps the sheet's height to leave that text
 * room above it; the writing sheet is not capped. Opening brings the passage into the space between the header and the sheet; a later move of the sheet (the keyboard, its
 * content settling) only lifts it out from under the sheet. Returns the stop, which gives back any room added below the transcript.
 */
export function keepAboveSheet(panel: HTMLElement | null, scroller: () => HTMLElement | null, passage: () => DOMRect | null, capText?: () => DOMRect | null): () => void {
	let held: HTMLElement | null = null;
	const stop = onSheetMoved(panel, opening => {
		const el = scroller(), text = passage();
		if (!panel || !el || !text) return;
		const regionTop = transcriptBounds(el)?.top ?? el.getBoundingClientRect().top;
		const pressed = capText?.();
		if (pressed) capSheetAboveText(panel, { height: pressed.height, regionTop });
		const sheetTop = restingTop(panel);
		if (sheetTop === null) return;
		held = el;
		placeAboveSheet(el, text, { top: regionTop, sheetTop }, opening);
	});
	return () => { stop(); if (held) giveBackRoom(held); };
}
