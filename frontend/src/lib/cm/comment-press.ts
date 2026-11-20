import type { EditorView } from '@codemirror/view';
import type { CommentRange } from './comment-marks';

/**
 * The highlight under a press, read only from where it landed, never from the caret: a phone puts the caret at the nearest end of
 * the tapped word, which lies outside a highlight that stops short of its word, and beside a highlight when the tap was on the space
 * or punctuation next to it. The position nearest a press is a highlight's end both when the press is on that end of its last
 * character and when it is on the character after, and a soft wrap makes one position the end of a row and the start of the next,
 * so at an end the press must also be on the highlight's side of it and on that end's own row.
 */
export function highlightAtPress(view: EditorView, press: { clientX: number; clientY: number }, ranges: CommentRange[]): CommentRange | undefined {
	return pickHighlight(highlightsAtPress(view, press, ranges), true);
}

/**
 * A touch tap puts the caret in the page's selection a moment before the editor reads it, and a card that opens in between takes the focus
 * first, so the editor keeps the caret it had (the document's start) and the first key typed lands there. Where the editor's caret is not in
 * the highlight the press opened, put it where the press landed, held inside that highlight; a caret the editor already placed in it stands.
 * The highlight is looked up by `id` in `ranges`, the list the card reads, so its range is where the text is now, not where it was when the
 * press landed: text can move in between, and the position under the old coordinates may then be outside it. Gone from the list: leave the caret.
 */
export function caretAtPress(view: EditorView, press: { clientX: number; clientY: number }, id: string, ranges: CommentRange[]): void {
	const now = ranges.find((r) => r.id === id);
	if (!now) return;
	const head = view.state.selection.main.head;
	if (head >= now.from && head <= now.to) return;
	const at = view.posAtCoords({ x: press.clientX, y: press.clientY }, false);
	view.dispatch({ selection: { anchor: Math.min(Math.max(at, now.from), now.to, view.state.doc.length) }, userEvent: 'select.pointer' });
}

/** Every highlight under a press by the same rule, in range order: two threads on overlapping text are both there. */
export function highlightsAtPress(view: EditorView, press: { clientX: number; clientY: number }, ranges: CommentRange[]): CommentRange[] {
	const at = view.posAtCoords({ x: press.clientX, y: press.clientY }, false);
	const onRow = (edge: { top: number; bottom: number } | null) => !edge || (press.clientY >= edge.top && press.clientY <= edge.bottom);
	return ranges.filter((r) => {
		if (r.point || at < r.from || at > r.to) return false;
		if (at === r.to && r.to > r.from) {
			const edge = view.coordsAtPos(r.to, -1);
			return onRow(edge) && press.clientX <= (edge?.right ?? Infinity);
		}
		if (at === r.from) {
			const edge = view.coordsAtPos(r.from, 1);
			return onRow(edge) && press.clientX >= (edge?.left ?? -Infinity);
		}
		return true;
	});
}

/**
 * The one rule for which highlight the reader is in, for a press and for a caret that arrived by keys alike. Of the highlights held at
 * a position, the one covering the least text: a one-word comment inside a longer suggestion's quote is what the reader pressed. Where
 * two cover the same text the suggestion wins, as the one the reader acts on, never whichever is listed first; the other stays
 * reachable from the comment list. A press opens a suggestion's card in every card mode. Only a caret that arrived by keys opens no
 * suggestion's card on the phone's sheet (`suggestionOpens` false there), where a comment is preferred to a suggestion however they
 * nest, so a caret never lands on a highlight that opens nothing while another one at that spot could open.
 */
export function pickHighlight(held: CommentRange[], suggestionOpens: boolean): CommentRange | undefined {
	const openable = suggestionOpens ? held : held.filter((r) => !r.suggestion);
	const narrower = (r: CommentRange, best: CommentRange) => r.to - r.from < best.to - best.from || (r.to - r.from === best.to - best.from && !!r.suggestion && !best.suggestion);
	return (openable.length ? openable : held).reduce<CommentRange | undefined>((best, r) => (!best || narrower(r, best) ? r : best), undefined);
}
