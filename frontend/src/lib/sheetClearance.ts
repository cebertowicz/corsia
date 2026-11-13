/** The page kept between the text a sheet was opened from and the sheet's top edge, and between that text and the top of the visible page. */
export const SHEET_GAP = 16;
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
