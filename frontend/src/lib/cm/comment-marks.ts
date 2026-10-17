import { StateField, StateEffect } from '@codemirror/state';
import { Decoration, type DecorationSet, EditorView } from '@codemirror/view';
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

// The scrolling pane can clip an editor that grows to the full content height.
const clipping = new Set(['auto', 'scroll', 'hidden', 'clip']);

function visibleBand(view: EditorView) {
	let top = 0;
	let bottom = view.dom.ownerDocument.defaultView?.innerHeight ?? window.innerHeight;
	for (let element: HTMLElement | null = view.scrollDOM; element; element = element.parentElement) {
		if (!clipping.has(getComputedStyle(element).overflowY)) continue;
		const rect = element.getBoundingClientRect();
		top = Math.max(top, rect.top);
		bottom = Math.min(bottom, rect.bottom);
	}
	// A phone drawer overlays rather than clips the editor. Its current resting top is therefore part of the visible
	// band: a jump must leave the target above it, not briefly behind it while the drawer settles.
	const drawer = view.dom.ownerDocument.querySelector<HTMLElement>('[data-doc-comments-drawer]');
	if (drawer) bottom = Math.min(bottom, drawer.getBoundingClientRect().top);
	return { top, bottom };
}

/** The nearest scrolling ancestor of the editor: the pane, when the editor's own scroller grows to the full content height. */
function scrollerOf(view: EditorView): HTMLElement {
	for (let element: HTMLElement | null = view.scrollDOM; element; element = element.parentElement) {
		const overflow = getComputedStyle(element).overflowY;
		if ((overflow === 'auto' || overflow === 'scroll') && element.scrollHeight > element.clientHeight + 1) return element;
	}
	return view.scrollDOM;
}

const QUOTE_MARGIN = 8;
type Box = { top: number; bottom: number };
const within = (box: Box | null, band: Box) => !!box && box.top >= band.top && box.bottom <= band.bottom;

export function scrollToRange(view: EditorView, from: number, _to: number, origin: ScrollOrigin = 'navigation') {
	const target = Math.min(from, view.state.doc.length);
	const widget = widgetTarget(view, target);
	const quote = origin === 'quote-tap';
	// Where the target is drawn now: a text position, or its row in a table (a diagram, whole). Null while it is not drawn.
	const boxOf = (current: EditorView): Box | null => widget ? widget.box(current) : current.coordsAtPos(Math.min(target, current.state.doc.length));
	if (origin === 'comment-save' && within(boxOf(view), visibleBand(view))) return;
	// A drawn widget row is placed with the scroller inside CodeMirror's measure cycle: CodeMirror's own scroll target is the
	// whole replaced block, which centres on the block's middle. Anything else, and a widget not drawn yet, goes to CodeMirror.
	const jump = () => {
		if (widget?.box(view)) {
			view.requestMeasure({
				key: widget,
				read: (current) => ({ box: widget.box(current), band: visibleBand(current) }),
				write: ({ box, band }, current) => {
					widget.reveal(current);
					if (box) scrollerOf(current).scrollTop += box.top - (quote ? band.top + QUOTE_MARGIN : (band.top + band.bottom - (box.bottom - box.top)) / 2);
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
				return within(boxOf(current), visibleBand(current));
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
