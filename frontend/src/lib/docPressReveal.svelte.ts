import type { EditorView } from '@codemirror/view';
import { pressSpan } from '$lib/cm/comment-marks';
import { visibleRegion } from '$lib/cm/visible-region';
import { releasePlace } from '$lib/documentReadingPlace';
import { capSheetAboveText, giveBackRoom, placeAboveSheet } from '$lib/sheetClearance';

type Span = { from: number; to: number };
type Scroll = { from: number; to: number; origin: 'comment-save' };

/**
 * Keeps the text a comment card was opened from in the band above a phone's sheet. `notePress` records the pressed spot when it lies on
 * the thread's text, else where the thread begins (a table cell has no spot of its own); `revealCard` caps the sheet above that text
 * and puts the text there (`placeAboveSheet`, as the chat transcript does); `revealDraft` keeps a draft's text above the phone draft sheet.
 * Call it from a component's setup.
 */
export function createPressReveal(deps: {
	highlights: () => (Span & { id: string })[];
	view: () => EditorView | undefined;
	draftCovered: (from: number, opening: boolean) => boolean | undefined;
	scrollTo: (to: Scroll) => void;
	draft: () => Span | null | undefined;
	sheetUp: () => boolean;
}) {
	let pressedAt = $state<number | null>(null);
	let pressedRange = $state<Span | null>(null);
	let placed: HTMLElement | null = null;
	// Once the sheet is gone: gives back the room a reveal added below the document, and forgets the press, so a comment opened another way (a saved draft) is not placed from an old one.
	$effect(() => { if (deps.sheetUp()) return; pressedAt = null; pressedRange = null; if (placed) giveBackRoom(placed); placed = null; });
	return {
		notePress(ids: string[], click?: MouseEvent) {
			const range = deps.highlights().find((h) => h.id === ids[0]);
			const at = click && click.detail !== 0 ? deps.view()?.posAtCoords({ x: click.clientX, y: click.clientY }, false) : null;
			pressedAt = range ? (at != null && at >= range.from && at <= range.to ? at : range.from) : null;
			pressedRange = range ? { from: range.from, to: range.to } : null;
		},
		/** The sheet rises only as far as leaves the pressed highlight above it (up to three lines of it, else the pressed line), and the document puts that text there. */
		revealCard(opening: boolean, panel: HTMLElement | null) {
			const at = pressedAt, range = pressedRange, view = deps.view();
			const span = at !== null && range && view ? pressSpan(view, range, at) : null;
			if (!span || !view) return;
			if (panel) capSheetAboveText(panel, span);
			const { band, scroller } = visibleRegion(view);
			placed = scroller;
			placeAboveSheet(scroller, span, { top: band.top, sheetTop: band.bottom }, opening);
			// The reader's place is taken again from where this leaves them, not held where the document was.
			releasePlace(view);
		},
		/** The phone draft sheet covers the editor's lower part: keep the draft's text where the band above the sheet shows it, on open and once a movement covers it. */
		revealDraft(opening: boolean) {
			const draft = deps.draft();
			if (draft && (deps.draftCovered(draft.from, opening) ?? opening)) deps.scrollTo({ from: draft.from, to: draft.to, origin: 'comment-save' });
		},
	};
}
