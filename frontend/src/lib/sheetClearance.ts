/** The page kept between the text a sheet was opened from and the sheet's top edge, and between that text and the top of the visible page. */
export const SHEET_GAP = 16;
/** The tallest pressed highlight, in lines, that a sheet keeps whole above itself; a taller one keeps only the pressed line. */
const PRESS_LINES = 3;
/** Whether a highlight `textHeight` tall is kept whole above a sheet, given the height of one line of it. One rule for the chat sheet, the document sheet and the wide card. */
export const keptWhole = (textHeight: number, lineHeight: number): boolean => textHeight <= PRESS_LINES * lineHeight;
/** A sheet is never capped below this, so its content stays usable; text that cannot get room above it is then partly covered. */
const MIN_SHEET = 200;

/**
 * Lets a bottom sheet rise only as far as leaves room above it for the text it was opened from (`height` tall, with a gap each side), under
 * the top of the visible page (`regionTop`). The sheet's own content scrolls. A sheet that is shorter than the room is not changed.
 * Idempotent: it depends only on the sheet's bottom edge, never on where the page is scrolled.
 */
export function capSheetAboveText(panel: HTMLElement, text: { height: number; regionTop: number }): void {
	const backdrop = panel.closest<HTMLElement>('[data-adaptive-drawer]');
	if (!backdrop) return;
	const room = backdrop.getBoundingClientRect().bottom - (text.regionTop + SHEET_GAP + text.height + SHEET_GAP);
	panel.style.setProperty('--drawer-text-cap', `${Math.max(MIN_SHEET, Math.floor(room))}px`);
}

/**
 * A line near the end of a scroll area cannot be lifted by `move` without room below it. Adds the missing room to the scroller's bottom
 * padding and says whether it did, for the caller to give back (with the padding it had) once the sheet it was for is gone. The one
 * place that adds this room, for the chat transcript and the document alike.
 */
export function addRoomBelow(scroller: HTMLElement, move: number): boolean {
	const missing = move - (scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop);
	if (missing <= 0) return false;
	scroller.style.paddingBottom = `${parseFloat(getComputedStyle(scroller).paddingBottom) + Math.ceil(missing)}px`;
	return true;
}
