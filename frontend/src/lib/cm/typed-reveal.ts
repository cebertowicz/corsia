import { type ChangeSet } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { commentRanges } from './comment-marks';
import { typingDriver } from './typed-reveal-driver';
import { arrivalSelects } from './typed-reveal-arrival';
import {
	authorTagger, revealAll, revealThrough, typedReveals, typedRevealOptions,
	type TypedRevealOptions,
} from './typed-reveal-state';

export { LABEL_FADE_MS, TYPING_IDLE_MS } from './typed-reveal-driver';
export { typedReveals, type Lane } from './typed-reveal-state';

function prefersReducedMotion(): boolean {
	return typeof window !== 'undefined' && (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);
}

/**
 * An agent live write types in at that agent caret. The server names the writer on
 * the update; the transaction that applying it produces carries the inserted ranges.
 * `canType` adds the host conditions (a live, connected session); reduced motion and
 * a hidden tab are always honored.
 */
export function createTypedReveal(options: { canType?: () => boolean; isInView?: TypedRevealOptions['isInView'] } = {}) {
	const ref: { view?: EditorView } = {};
	const resolved: TypedRevealOptions = {
		canType: () => !prefersReducedMotion() && document.visibilityState !== 'hidden' && (options.canType?.() ?? true),
		isInView: options.isInView ?? ((changes: ChangeSet, from: number, to: number) => {
			const view = ref.view;
			if (!view) return true;
			return view.visibleRanges.some((range) => from < changes.mapPos(range.to, -1) && to > changes.mapPos(range.from, -1));
		}),
	};
	const tracker = EditorView.updateListener.of((update) => { ref.view = update.view; });
	// The lanes read the comment ranges (a committed suggestion claims its preview); the editor's comment marks share this field.
	return [typedRevealOptions.of(resolved), commentRanges, authorTagger, typedReveals, arrivalSelects, typingDriver, tracker];
}

/** Show everything still hidden, in every lane (disconnect, reconnect, tab hidden). */
export function settleTypedReveals(view: EditorView): void {
	view.dispatch({ effects: revealAll.of(null) });
}

/** Show every hidden character before `position` at once. */
export function revealTypedThrough(view: EditorView, position: number): void {
	view.dispatch({ effects: revealThrough.of(position) });
}

/**
 * Show the hidden text under any of these ranges at once, so a comment highlight or
 * scroll target never sits on text that is still typing in. Hidden text elsewhere
 * keeps typing.
 */
export function revealTypedUnder(view: EditorView, ranges: ReadonlyArray<{ from: number; to: number }>): void {
	let through = -1;
	for (const lane of view.state.field(typedReveals, false) ?? []) {
		for (const segment of lane.segments) {
			for (const range of ranges) {
				if (range.from < segment.to && range.to > segment.from) through = Math.max(through, Math.min(range.to, segment.to));
			}
		}
	}
	if (through >= 0) revealTypedThrough(view, through);
}
