/** Where a commented passage can be seen in the transcript: below the shell header, above the chat composer or a comment sheet. */
import { restingTop } from '$lib/cm/visible-region';
import { onSheetMoved } from '$lib/docCommentDrawer';

const GAP = 8;
/** Space the transcript keeps at its end while a sheet is open over it (MessageList draws it), so the last row can scroll up clear of the sheet. */
export const SHEET_SPACE = '--comment-sheet-space';

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
 * Scrolls the transcript so the passage sits between the header and the sheet, as a document keeps its draft above its sheet.
 * Opening brings it into that space from anywhere; a later move of the sheet (the keyboard, its content settling) only lifts it
 * out from under the sheet, so a scroll the reader chose is not undone. The transcript keeps as much space at its end as the sheet
 * is tall, so even its last row can come up above the sheet; a scroll alone cannot move a transcript already at its end.
 */
export function revealAboveSheet(scroller: HTMLElement | null, passage: DOMRect | null, panel: HTMLElement, opening: boolean): void {
	const sheetTop = restingTop(panel);
	if (!scroller || sheetTop === null) return;
	scroller.style.setProperty(SHEET_SPACE, `${panel.offsetHeight}px`);
	if (!passage) return;
	const top = (panel.ownerDocument.querySelector('.shell-header')?.getBoundingClientRect().bottom ?? 0) + GAP;
	const bottom = sheetTop - GAP;
	const room = bottom - top;
	if (room <= 0 || (passage.top >= top && passage.bottom <= bottom)) return;
	if (!opening && passage.bottom <= bottom) return;
	// A passage taller than the space shows from its first line; a shorter one is centred in it.
	const delta = passage.height > room ? passage.top - top : passage.top + passage.height / 2 - (top + room / 2);
	scroller.scrollBy({ top: delta });
}

/** Keeps the passage in view while a comment sheet is open over the transcript. Returns the stop, which gives the space back. */
export function keepAboveSheet(panel: HTMLElement | null, scroller: () => HTMLElement | null, passage: () => DOMRect | null): () => void {
	let spaced: HTMLElement | null = null;
	const stop = onSheetMoved(panel, opening => {
		if (!panel) return;
		spaced = scroller();
		revealAboveSheet(spaced, passage(), panel, opening);
	});
	return () => { stop(); spaced?.style.removeProperty(SHEET_SPACE); };
}
