import { StateEffect, Transaction } from '@codemirror/state';
import { Decoration, EditorView, WidgetType } from '@codemirror/view';
import { overlapsSpan, type Segment } from './typed-reveal-segments';

/**
 * The "select" step of an agent edit or comment, drawn like a person dragging a
 * selection: the caret jumps to the start, then a selection in the agent colour sweeps
 * across. Presentation only. `ghost` carries the old text of a replacement or deletion
 * (already gone from the document) as a display-only widget; it is never part of the doc.
 * The typed-reveal driver is the only clock for a run; `typed-reveal-arrival` hands a run's end back.
 */
export interface Selecting {
	/** Names one run: a comment's arrival id, or a generated key for a ghost. A lane's select is never replaced, so a key is one run. */
	key: string;
	/** Doc range being selected; `from === to` for a ghost, which sits where the old text was. */
	from: number;
	to: number;
	ghost: string | null;
	/** Characters selected so far, 0 to `length`. */
	shown: number;
	length: number;
	sweepMs: number;
	/** Hold after the sweep; the run ends on its own once sweep and hold have elapsed. */
	holdMs: number;
}

/** How a run ended. `unavailable` is every end the editor did not choose: lane dropped, anchor gone, editor destroyed. */
export type SelectOutcome = 'ended' | 'interrupted' | 'unavailable';

export const selectShown = StateEffect.define<{ threadId: string; key: string; shown: number }>();
/** Ends one run, at its natural end or because a user acted on it. */
export const endSelect = StateEffect.define<{ threadId: string; key: string; outcome: Exclude<SelectOutcome, 'unavailable'> }>();
/** Starts a comment run on a lane that has no select; the lane is created when the agent has none yet. */
export const beginSelect = StateEffect.define<{ threadId: string; name: string; color: string; select: Selecting }>();

/** The longest old text a select step sweeps; longer replacements show without one. */
export const GHOST_MAX_CHARS = 1_200;

let sequence = 0;
export const nextGhostKey = () => `ghost-${++sequence}`;

/** A user change or selection inside (or touching) the selected span ends the sequence at once. */
export function userTouchedSelect(transaction: Transaction, select: Selecting): boolean {
	if (transaction.annotation(Transaction.userEvent) === undefined && !transaction.selection) return false;
	let touched = false;
	if (transaction.docChanged && transaction.annotation(Transaction.userEvent) !== undefined) {
		transaction.changes.iterChangedRanges((fromA, toA) => { if (fromA <= select.to && toA >= select.from) touched = true; });
	}
	const selection = transaction.selection;
	if (selection?.ranges.some((range) => range.from <= select.to && range.to >= select.from)) touched = true;
	return touched;
}

/** The same user-action rule for the lane's pending typed text: touching it ends the select step too. */
export function userTouchedAny(transaction: Transaction, segments: readonly Segment[]): boolean {
	const byUser = transaction.docChanged && transaction.annotation(Transaction.userEvent) !== undefined;
	return segments.some((segment) => {
		let touched = !!transaction.selection?.ranges.some((range) => overlapsSpan(segment, range.from, range.to));
		if (byUser) transaction.changes.iterChangedRanges((fromA, toA) => { if (overlapsSpan(segment, fromA, toA)) touched = true; });
		return touched;
	});
}

/** Old text selected in the agent colour; `shown` characters from the start are highlighted. */
export class GhostSelection extends WidgetType {
	constructor(private readonly text: string, private readonly shown: number, private readonly color: string) { super(); }

	eq(other: GhostSelection): boolean {
		return this.text === other.text && this.shown === other.shown && this.color === other.color;
	}

	toDOM(): HTMLElement {
		const ghost = document.createElement('span');
		ghost.className = 'cm-presence-ghost';
		ghost.setAttribute('aria-hidden', 'true');
		ghost.style.setProperty('--presence-color', this.color);
		const selected = document.createElement('span');
		selected.className = 'cm-presence-ghost-selected';
		selected.textContent = this.text.slice(0, this.shown);
		ghost.append(selected, document.createTextNode(this.text.slice(this.shown)));
		return ghost;
	}

	ignoreEvent(): boolean { return true; }
}

export function selectMarks(select: Selecting, color: string, threadId: string) {
	if (select.ghost !== null) {
		return [Decoration.widget({ widget: new GhostSelection(select.ghost, select.shown, color), side: -2 }).range(select.from)];
	}
	if (select.shown <= 0) return [];
	return [Decoration.mark({
		class: 'cm-presence-select',
		attributes: { style: `--presence-color:${color}`, 'data-thread-id': threadId },
	}).range(select.from, select.from + select.shown)];
}

/** Where the caret sits while a sequence runs: the head of the sweep. */
export const selectHead = (select: Selecting) => select.ghost !== null ? select.from : select.from + select.shown;

export const selectTheme = EditorView.baseTheme({
	'.cm-presence-select': {
		backgroundColor: 'color-mix(in srgb, var(--presence-color) 38%, transparent)',
		borderRadius: '2px',
	},
	'.cm-presence-ghost': {
		userSelect: 'none',
		pointerEvents: 'none',
		textDecoration: 'none',
	},
	'.cm-presence-ghost-selected': {
		backgroundColor: 'color-mix(in srgb, var(--presence-color) 38%, transparent)',
		borderRadius: '2px',
	},
});
