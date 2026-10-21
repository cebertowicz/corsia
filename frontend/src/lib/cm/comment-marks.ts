import { StateField, StateEffect } from '@codemirror/state';
import { Decoration, type DecorationSet, EditorView } from '@codemirror/view';
import { visibleRegion, type Box } from './visible-region';
import { widgetTarget } from './widget-scroll';

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
	replyWaiting?: boolean;
	onMarkerFocus?: (ids: string[], opener: HTMLElement, anchor?: () => DOMRect | null) => void;
	suggestion?: InlineSuggestion;
}

export const setCommentRanges = StateEffect.define<CommentRange[]>();

const anchor = Decoration.mark({ class: 'cm-comment-anchor' });
const anchorFocused = Decoration.mark({ class: 'cm-comment-anchor cm-comment-focused' });

// A suggestion with an empty quote is a pure insertion: its range is a point.
const located = (range: CommentRange) => range.from < range.to || (range.suggestion !== undefined && range.from === range.to);
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
			if (effect.is(setCommentRanges)) ranges = effect.value.filter(located);
		}
		if (!tr.docChanged) return ranges;
		return ranges
			.map((range) => ({ ...range, from: tr.changes.mapPos(range.from, 1), to: tr.changes.mapPos(range.to, -1) }))
			.filter((range) => located(range) && holdsQuote(range, tr.newDoc));
	}
});

/** The anchor highlights, derived from `commentRanges`. A suggestion's range has none: its own marks replace it. */
export const commentMarks = [
	commentRanges,
	EditorView.decorations.compute([commentRanges], (state): DecorationSet =>
		Decoration.set(state.field(commentRanges).filter((range) => !range.suggestion)
			.map((range) => (range.focused ? anchorFocused : anchor).range(range.from, range.to)), true)),
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

export function scrollToRange(view: EditorView, from: number, _to: number, origin: ScrollOrigin = 'navigation') {
	const target = Math.min(from, view.state.doc.length);
	const widget = widgetTarget(view, target);
	const quote = origin === 'quote-tap';
	const boxOf = (current: EditorView) => boxAt(current, target, widget);
	if (origin === 'comment-save' && within(boxOf(view), visibleRegion(view).band)) return;
	// A drawn widget row is placed with the scroller inside CodeMirror's measure cycle: CodeMirror's own scroll target is the
	// whole replaced block, which centres on the block's middle. Anything else, and a widget not drawn yet, goes to CodeMirror.
	const jump = () => {
		if (widget?.box(view)) {
			view.requestMeasure({
				key: widget,
				read: (current) => ({ box: widget.box(current), ...visibleRegion(current) }),
				write: ({ box, band, scroller }, current) => {
					widget.reveal(current);
					if (box) scroller.scrollTop += box.top - (quote ? band.top + QUOTE_MARGIN : (band.top + band.bottom - (box.bottom - box.top)) / 2);
				},
			});
			return;
		}
		view.dispatch({ effects: EditorView.scrollIntoView(widget?.start ?? target, quote || widget ? { y: 'start', yMargin: QUOTE_MARGIN } : { y: 'center' }) });
	};
	const nextFrame = (run: () => void) => view.dom.ownerDocument.defaultView?.requestAnimationFrame(() => {
		if (view.dom.isConnected) run();
	});
	let steady = 0;
	// CodeMirror refuses a dispatch during its measure cycle, so a repeat jump
	// waits for the next frame.
	const check = (pass: number) => nextFrame(() => {
		view.requestMeasure({
			key: scrollToRange,
			read(current) {
				return within(boxOf(current), visibleRegion(current).band);
			},
			write(onScreen) {
				if (onScreen) {
					steady += 1;
					if (steady < STEADY_CHECKS) check(pass);
					return;
				}
				steady = 0;
				if (pass >= MAX_SCROLL_PASSES) return;
				nextFrame(() => {
					jump();
					check(pass + 1);
				});
			},
		});
	});
	jump();
	check(0);
}
