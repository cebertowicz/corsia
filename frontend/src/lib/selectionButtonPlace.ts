/** Below this distance from the window's top, the space above a selection is the app's header, not the page. */
const ROOM_ABOVE = 96;
const GAP = 6;

/**
 * Where a selection's Comment button sits in a chat or a document, so it never covers the selected text: above the box
 * the selection is drawn in, or below it on a touch screen (where the platform's own selection menu takes the space
 * above) and when the box is near the window's top. `top` is the button's edge nearest the selection.
 */
export function selectionButtonPlace(box: { top: number; bottom: number }, coarse: boolean): { top: number; above: boolean } {
	const above = !coarse && box.top > ROOM_ABOVE;
	return { top: above ? box.top - GAP : box.bottom + GAP, above };
}

/**
 * `top` held inside the band the reader can see, so a selection taller than the window, or one the document has scrolled away
 * from, keeps its button on screen at the band's edge nearest the selection. A button placed `above` hangs up from `top`, one
 * placed below hangs down from it, so each keeps its whole height inside the band.
 */
export function keepInBand(place: { top: number; above: boolean }, band: { top: number; bottom: number }, height: number, edge: number): number {
	const least = band.top + edge + (place.above ? height : 0);
	const most = Math.max(least, band.bottom - edge - (place.above ? 0 : height));
	return Math.min(Math.max(place.top, least), most);
}

export const coarsePointer = (): boolean => typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
