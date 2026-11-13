import { StateField, StateEffect, type EditorState } from '@codemirror/state';
import { Decoration, type DecorationSet, EditorView } from '@codemirror/view';
import { visibleRegion, type Box } from './visible-region';
import { widgetTarget } from './widget-scroll';
import { releasePlace } from '$lib/documentReadingPlace';
import type { ImageRegion } from '$lib/api/docs';
import { mapCommentRange } from './comment-range';

/** An open suggestion on a comment range (`suggestion-marks.ts` draws it in place of the anchor highlight). */
export interface InlineSuggestion {
	/** The text the range must still hold: a local edit that changes it ends the suggestion at once. */
	quote: string;
	insert: string;
	/** The suggesting agent's presence colour. */
	color: string;
	/** False for a suggestion overlapped by an earlier open one: card only, tinted only while focused. */
	inline: boolean;
	/** Its Accept is in flight. */
	busy?: boolean;
}

export interface CommentRange {
	from: number;
	to: number;
	id: string;
	focused?: boolean;
	onMarkerFocus?: (ids: string[], opener: HTMLElement, anchor?: () => DOMRect | null, click?: MouseEvent) => void;
	suggestion?: InlineSuggestion;
	/** A point, not a passage: drawn as a margin marker only, with no highlight (its text was deleted, or an accepted suggestion deleted it). */
	point?: boolean;
	/** A pin on an image or diagram region: drawn by its pin, never as a highlight on the whole block. */
	region?: boolean;
	/** What that pin draws (`image-pins.ts`): its box or point on the image, its number and its author's colour. */
	pin?: { region: ImageRegion; number: number; color: string };
	/** The thread has something the reader has not opened, the comment itself or a reply: its highlight carries the new sign, named for it (`comment-new-signs.ts`). */
	unread?: 'comment' | 'reply';
	/** Its text was just typed over: the rest of the word typed at its end joins it (`mapCommentRange`). */
	open?: boolean;
}

export const setCommentRanges = StateEffect.define<CommentRange[]>();

/**
 * What the editor repaints on: the ranges are sent to it again only when this changes. It is the ranges' own data, every field of
 * each, so a field added to `CommentRange` is in it without being listed here. Only the callbacks are left out (JSON has no
 * functions): they are new closures on every build and draw nothing.
 */
export const paintSignature = (ranges: CommentRange[]): string => JSON.stringify(ranges);

/**
 * The highlight under a press, read only from where it landed, never from the caret: a phone puts the caret at the nearest end of
 * the tapped word, which lies outside a highlight that stops short of its word, and beside a highlight when the tap was on the space
 * or punctuation next to it. The position nearest a press is a highlight's end both when the press is on that end of its last
 * character and when it is on the character after, and a soft wrap makes one position the end of a row and the start of the next,
 * so at an end the press must also be on the highlight's side of it and on that end's own row.
 */
export function highlightAtPress(view: EditorView, press: { clientX: number; clientY: number }, ranges: CommentRange[], tapOpensSuggestion = true): CommentRange | undefined {
	const at = view.posAtCoords({ x: press.clientX, y: press.clientY }, false);
	const onRow = (edge: { top: number; bottom: number } | null) => !edge || (press.clientY >= edge.top && press.clientY <= edge.bottom);
	return pickHighlight(ranges.filter((r) => {
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
	}), tapOpensSuggestion);
}

/**
 * The one rule for which highlight the reader is in, for a press and for a caret that arrived by keys alike. Of the highlights held at
 * a position, the one covering the least text, the earliest on a tie: a one-word comment inside a longer suggestion's quote is what the
 * reader pressed. Where a tap opens no suggestion's card (the phone's sheet), a comment is preferred to a suggestion however they nest,
 * so a tap never lands on a highlight that opens nothing while another one at that spot could open.
 */
export function pickHighlight(held: CommentRange[], suggestionOpens: boolean): CommentRange | undefined {
	const openable = suggestionOpens ? held : held.filter((r) => !r.suggestion);
	return (openable.length ? openable : held).reduce<CommentRange | undefined>((best, r) => (!best || r.to - r.from < best.to - best.from ? r : best), undefined);
}

const anchor = Decoration.mark({ class: 'cm-comment-anchor' });
const anchorFocused = Decoration.mark({ class: 'cm-comment-anchor cm-comment-focused' });
/** An unread thread's highlight names its thread, so its sign can find where the highlight ends. */
const markOf = (range: CommentRange) => !range.unread ? (range.focused ? anchorFocused : anchor)
	: Decoration.mark({ class: range.focused ? 'cm-comment-anchor cm-comment-focused' : 'cm-comment-anchor', attributes: { 'data-comment-new': range.id, 'data-unread': '' } });

// A suggestion with an empty quote is a pure insertion: its range is a point.
const located = (range: CommentRange) => range.from < range.to || (range.suggestion !== undefined && range.from === range.to) || (!!range.point && range.from === range.to);
const holdsQuote = (range: CommentRange, text: { sliceString(from: number, to: number): string }) =>
	!range.suggestion || text.sliceString(range.from, range.to) === range.suggestion.quote;

/**
 * The located threads, in the order the document gives them: the one owner of comment ranges. The highlights
 * and the margin rail both read it, so an edit that collapses an anchor removes its highlight and its marker
 * together.
 */
export const commentRanges = StateField.define<CommentRange[]>({
	create() { return []; },
	update(ranges, tr) {
		for (const effect of tr.effects) {
			// The comments' own positions arrive without the open end the typing here gave a range: it stays while they agree.
			if (effect.is(setCommentRanges)) ranges = effect.value.filter(located).map((range) =>
				ranges.some((was) => was.open && was.id === range.id && was.from === range.from && was.to === range.to) ? { ...range, open: true } : range);
		}
		if (!tr.docChanged) return ranges;
		return ranges
			// A pure insertion's empty range is neither text nor a point: text typed at it ends it, as before.
			.map((range) => range.point || range.from < range.to
				? { ...range, ...mapCommentRange(range.point ? { from: range.from, to: range.from } : range, tr.changes) }
				: { ...range, from: tr.changes.mapPos(range.from, 1), to: tr.changes.mapPos(range.to, -1) })
			.filter((range) => located(range) && holdsQuote(range, tr.newDoc));
	}
});

/** The comments whose text was just typed over in `state`: the one owner of an editor's open ends, which the cards follow. */
export const openCommentIds = (state: EditorState): ReadonlySet<string> =>
	new Set(state.field(commentRanges, false)?.filter((range) => range.open).map((range) => range.id));

/** The anchor highlights, derived from `commentRanges`. A suggestion's range has none: its own marks replace it. */
export const commentMarks = [
	commentRanges,
	EditorView.decorations.compute([commentRanges], (state): DecorationSet =>
		Decoration.set(state.field(commentRanges).filter((range) => !range.suggestion && !range.point)
			.map((range) => markOf(range).range(range.from, range.to)), true)),
];

// A far jump lands on estimated line heights. The lines it reveals are measured
// afterwards, taller, and CodeMirror's scroll anchoring keeps the top line
// still, so the target drifts down (about 1,200px on a 70-paragraph document).
// Check in a later frame, once the scroll is applied and the revealed lines are
// measured, and jump again until the target stays on screen.
export const MAX_SCROLL_PASSES = 8;
const STEADY_CHECKS = 2;

/** `quote-tap` leaves a quoted passage at the top of the canvas for the half-height comment sheet. */
export type ScrollOrigin = 'navigation' | 'comment-save' | 'quote-tap';

const QUOTE_MARGIN = 8;
const within = (box: Box | null, band: Box) => !!box && box.top >= band.top && box.bottom <= band.bottom;

// Where a target is drawn now: a text position, or its row in a table (a diagram, whole). Null while it is not drawn.
const boxAt = (current: EditorView, target: number, widget: ReturnType<typeof widgetTarget>): Box | null => widget ? widget.box(current) : current.coordsAtPos(Math.min(target, current.state.doc.length));

/**
 * Whether a sheet or keyboard movement has just covered the text at `from`: the reader had it in view before, inside `before` (the
 * band as of the last movement), and the band now leaves it out. Text the reader had scrolled out of view is not covered by a movement.
 * Returns the band now, which is the next movement's `before`.
 */
export function coveredByMove(view: EditorView, from: number, before: Box | null): { band: Box; covered: boolean } {
	const { band } = visibleRegion(view);
	const target = Math.min(from, view.state.doc.length), box = boxAt(view, target, widgetTarget(view, target));
	return { band, covered: !!before && within(box, before) && !within(box, band) };
}

/** The frame watch of the jump that last moved the reader: one at a time, so a new jump retires the previous one. */
const placedIn = new WeakMap<EditorView, () => void>();

export function scrollToRange(view: EditorView, from: number, _to: number, origin: ScrollOrigin = 'navigation') {
	const target = Math.min(from, view.state.doc.length);
	const widget = widgetTarget(view, target);
	const quote = origin === 'quote-tap';
	const boxOf = (current: EditorView) => boxAt(current, target, widget);
	if (origin === 'comment-save' && within(boxOf(view), visibleRegion(view).band)) return;
	// With comments in the margin the cards move to the line, so a visible line never makes the document scroll.
	if (view.dom.closest('.doc')?.querySelector('.canvas-comment-drawer.margin') && within(boxOf(view), visibleRegion(view).band)) return;
	// A drawn widget row is placed with the scroller inside CodeMirror's measure cycle: CodeMirror's own scroll target is the
	// whole replaced block, which centres on the block's middle. Anything else, and a widget not drawn yet, goes to CodeMirror.
	const jump = () => {
		if (widget?.box(view)) {
			view.requestMeasure({
				key: widget,
				read: (current) => ({ box: widget.box(current), ...visibleRegion(current) }),
				write: ({ box, band, scroller }, current) => {
					if (done) return;
					widget.reveal(current);
					if (box) scroller.scrollTop += box.top - (quote ? band.top + QUOTE_MARGIN : (band.top + band.bottom - (box.bottom - box.top)) / 2);
				},
			});
			// The reader's place is taken again from where this lands.
			releasePlace(view);
			return;
		}
		view.dispatch({ effects: EditorView.scrollIntoView(widget?.start ?? target, quote || widget ? { y: 'start', yMargin: QUOTE_MARGIN } : { y: 'center' }) });
	};
	const nextFrame = (run: () => void) => view.dom.ownerDocument.defaultView?.requestAnimationFrame(() => {
		if (view.dom.isConnected) run();
	});
	let steady = 0;
	let settledPass = 0;
	let done = false;
	// The frame the reader is being placed in moves under a jump: opening the comments drawer takes width from the pane at
	// 1440, every line above the landing rewraps, and the landing shifts after the frame checks have stopped. Watch the
	// scroller's own box for as long as this jump holds the reader's place, and take the place again when that box moves;
	// a reader scroll ends the watch, so nothing here pulls the reader back. Its first callback only reports the size the
	// box already has, which is not a move.
	let seenSize = false;
	const stop = () => {
		done = true;
		watch?.disconnect();
		visibleRegion(view).scroller.removeEventListener('scroll', onMove);
		if (placedIn.get(view) === stop) placedIn.delete(view);
	};
	const onMove = () => stop();
	const watch = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
		if (!seenSize) { seenSize = true; return; }
		if (done) return;
		steady = 0;
		check(settledPass);
	}) : null;
	placedIn.get(view)?.();
	placedIn.set(view, stop);
	// CodeMirror refuses a dispatch during its measure cycle, so a repeat jump
	// waits for the next frame.
	// A retired jump may still have a frame or a measure queued: each one checks `done` first, so it never moves the reader.
	const check = (pass: number) => nextFrame(() => {
		if (done) return;
		settledPass = pass;
		view.requestMeasure({
			key: scrollToRange,
			read(current) {
				return within(boxOf(current), visibleRegion(current).band);
			},
			write(onScreen) {
				if (done) return;
				if (onScreen) {
					// A table drawn after the initial jump still needs its target cell revealed sideways.
					if (steady === 0) widget?.reveal(view);
					steady += 1;
					if (steady < STEADY_CHECKS) { check(pass); return; }
					// Settled. From here a scroll is the reader's own, and their place is theirs to keep.
					visibleRegion(view).scroller.addEventListener('scroll', onMove, { passive: true });
					return;
				}
				steady = 0;
				if (pass >= MAX_SCROLL_PASSES) { stop(); return; }
				nextFrame(() => {
					if (done) return;
					jump();
					check(pass + 1);
				});
			},
		});
	});
	jump();
	check(0);
	watch?.observe(visibleRegion(view).scroller);
}
