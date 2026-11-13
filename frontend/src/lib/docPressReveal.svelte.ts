import type { EditorView } from '@codemirror/view';
import { clearRevealRoom, pressSpan } from '$lib/cm/comment-marks';
import { capSheetAboveText } from '$lib/sheetClearance';

type Span = { from: number; to: number };
type Scroll = { from: number; to: number; origin: 'press-reveal' | 'comment-save' };

/** When the press that opened the sheet happened: its content is still settling for a moment after, and each settling move places the text again. */
const PRESS_SETTLE_MS = 1000;

/**
 * Keeps the text a comment card was opened from in the band above a phone's sheet. `notePress` records the pressed spot when it lies on
 * the thread's text, else where the thread begins (a table cell has no spot of its own); `revealCard` caps the sheet above that text
 * and lifts the document to put it there; `revealDraft` does the same for a draft's text under the phone draft sheet. Call it from a component's setup.
 */
export function createPressReveal(deps: {
	highlights: () => (Span & { id: string })[];
	view: () => EditorView | undefined;
	draftCovered: (from: number, opening: boolean, clear: boolean, to: number) => boolean | undefined;
	scrollTo: (to: Scroll) => void;
	draft: () => Span | null | undefined;
	sheetUp: () => boolean;
}) {
	let pressedAt = $state<number | null>(null);
	let pressedRange = $state<Span | null>(null);
	let pressedTime = 0;
	// Once the sheet is gone: gives back the room a reveal added below the document, and forgets the press, so a comment opened another way (a saved draft) is not placed from an old one.
	$effect(() => { if (deps.sheetUp()) return; pressedAt = null; pressedRange = null; const view = deps.view(); if (view) clearRevealRoom(view); });
	return {
		notePress(ids: string[], click?: MouseEvent) {
			const range = deps.highlights().find((h) => h.id === ids[0]);
			const at = click && click.detail !== 0 ? deps.view()?.posAtCoords({ x: click.clientX, y: click.clientY }, false) : null;
			pressedAt = range ? (at != null && at >= range.from && at <= range.to ? at : range.from) : null;
			pressedRange = range ? { from: range.from, to: range.to } : null;
			pressedTime = performance.now();
		},
		/** The sheet rises only as far as leaves the pressed highlight above it (up to three lines of it, else the pressed line). */
		revealCard(opening: boolean, panel: HTMLElement | null) {
			const at = pressedAt, range = pressedRange, view = deps.view();
			const span = at !== null && range && view ? pressSpan(view, range, at) : null;
			if (!span) return;
			if (panel) capSheetAboveText(panel, span);
			const settling = performance.now() - pressedTime < PRESS_SETTLE_MS;
			if (deps.draftCovered(span.from, opening || settling, true, span.to) ?? opening) deps.scrollTo({ from: span.from, to: span.to, origin: 'press-reveal' });
		},
		/** The phone draft sheet covers the editor's lower part: keep the draft's text where the band above the sheet shows it, on open and once a movement covers it. */
		revealDraft(opening: boolean) {
			const draft = deps.draft();
			if (draft && (deps.draftCovered(draft.from, opening, false, draft.from) ?? opening)) deps.scrollTo({ from: draft.from, to: draft.to, origin: 'comment-save' });
		},
	};
}
