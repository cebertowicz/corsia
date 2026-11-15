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
 * Idempotent: it depends only on the sheet's bottom edge, never on where the page is scrolled. Callers skip a sheet the reader pulled
 * up (`sheetRaised`), so it stays up.
 */
export function capSheetAboveText(panel: HTMLElement, text: { height: number; regionTop: number }): void {
	const backdrop = panel.closest<HTMLElement>('[data-adaptive-drawer]');
	if (!backdrop) return;
	const room = backdrop.getBoundingClientRect().bottom - (text.regionTop + SHEET_GAP + text.height + SHEET_GAP);
	panel.style.setProperty('--drawer-text-cap', `${Math.max(MIN_SHEET, Math.floor(room))}px`);
}

/** Sheets the reader pulled up past their cap; each one is forgotten with its element, when the sheet closes. */
const raised = new WeakSet<HTMLElement>();

/**
 * Whether `capSheetAboveText` holds a sheet below the height its content would take, so pulling its grabber up grows it: the sheet
 * rests at its cap and its body has more content than it shows. The cap is set on every opened comment, but a short thread under a
 * tall cap is not held, and a sheet held by the screen's own limit is not held by the cap. Read from the layout as it stands: nothing
 * is changed to measure it, so no transition starts.
 */
export function sheetCapped(panel: HTMLElement | null): boolean {
	if (!panel || raised.has(panel)) return false;
	const cap = Number.parseFloat(panel.style.getPropertyValue('--drawer-text-cap'));
	const body = panel.querySelector<HTMLElement>(':scope > .drawer-body');
	if (!Number.isFinite(cap) || !body) return false;
	return Math.abs(panel.getBoundingClientRect().height - cap) <= 1 && body.scrollHeight - body.clientHeight > 1;
}

/** Sheets whose grabber is held, or that are settling after it was let go. */
const pulled = new WeakSet<HTMLElement>();

/** A grabber drag holds the sheet from its press until it settles: while held, nothing places the page or caps the sheet for it (`onSheetMoved`). */
export function holdSheet(panel: HTMLElement, held: boolean): void {
	if (held) pulled.add(panel);
	else pulled.delete(panel);
}

/** Whether a grabber drag holds this sheet. */
export const sheetHeld = (panel: HTMLElement | null): boolean => !!panel && pulled.has(panel);

/** Whether the reader pulled this sheet up: until it closes, nothing caps it again or moves the page for it. */
export const sheetRaised = (panel: HTMLElement | null): boolean => !!panel && raised.has(panel);

/** The reader pulled a capped sheet up: it grows to its full height, over the text if it must, and stays there until it closes. */
export function liftSheetCap(panel: HTMLElement): void {
	raised.add(panel);
	panel.style.removeProperty('--drawer-text-cap');
}

const roomAdded = new WeakMap<HTMLElement, string>();

/**
 * Scrolls `scroller` so `text` (a box in viewport coordinates) sits between `band.top` and the top of a sheet (`band.sheetTop`), a gap clear
 * on each side: not at all when it is there already, else centred in that space (from its first line when it is taller than the space).
 * A line near the end of a scroll area cannot be lifted without room below it, so the missing room is added to the scroller's bottom
 * padding until `giveBackRoom`. `opening` is the sheet's first placement; a later movement (the keyboard, the sheet's content settling)
 * only lifts text out from under the sheet, so a scroll the reader chose is not undone. The one placement and the one room-below
 * mechanism, for the chat transcript and the document alike.
 */
export function placeAboveSheet(scroller: HTMLElement, text: { top: number; bottom: number }, band: { top: number; sheetTop: number }, opening: boolean): void {
	const top = band.top + SHEET_GAP, bottom = band.sheetTop - SHEET_GAP, room = bottom - top, height = text.bottom - text.top;
	if (room <= 0 || (text.top >= top && text.bottom <= bottom) || (!opening && text.bottom <= bottom)) return;
	const move = height > room ? text.top - top : text.top + height / 2 - (top + room / 2);
	const missing = move - (scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop);
	if (missing > 0) {
		if (!roomAdded.has(scroller)) roomAdded.set(scroller, scroller.style.paddingBottom);
		scroller.style.paddingBottom = `${parseFloat(getComputedStyle(scroller).paddingBottom) + Math.ceil(missing)}px`;
	}
	scroller.scrollTop += move;
}

/** Gives back the room `placeAboveSheet` added below the scroller, once the sheet it was for is gone. */
export function giveBackRoom(scroller: HTMLElement): void {
	const before = roomAdded.get(scroller);
	if (before === undefined) return;
	scroller.style.paddingBottom = before;
	roomAdded.delete(scroller);
}

/** Fired on the document when the keyboard's settling has moved the chat transcript's layout while a comment sheet is open; the sheet then places its passage again. */
export const TRANSCRIPT_MOVED = 'corsia:transcript-moved';
