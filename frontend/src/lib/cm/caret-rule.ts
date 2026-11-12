import { Transaction } from '@codemirror/state';

/**
 * Whether a transaction is the reader moving the caret by something other than the press itself: it changes the document with a
 * caret of its own, or names a user event outside the 'select' family (a paste, dictation or autocorrect, undo or redo, a deletion, a caret
 * move). The 'select' family is the press itself ('select.pointer') and how a browser reports the caret a press left ('select'),
 * and an unannotated selection-only report is the same: none of them is one.
 */
export function releasesPress(tr: Transaction): boolean {
	const event = tr.annotation(Transaction.userEvent);
	return event !== undefined ? !tr.isUserEvent('select') : tr.docChanged && !!tr.selection;
}

/**
 * Which highlight the reader is in: one rule, one record (`id`), and the one owner of press precedence. It replaces the editor's
 * `caretIn` and `pendingPress` and its separate "pointer is down" flag.
 *
 * A pointer press decides it, from where the press landed (a phone puts the caret beside the tapped word, not under the finger),
 * and that answer stands against selection-only reports, a late 'select' for the same press included, until the caret is moved by
 * something else (`released`: a key, or a transaction `releasesPress`) or a later press. A caret read with no press looks the
 * highlight up from the caret; typing at a highlight's edge is editing there, never an arrival.
 *
 * States: idle -> down (pressStarted) -> waiting (pressUp: the editor has not yet placed the caret) -> latched (settled, then read
 * with the press) -> idle (released, or range). down and waiting hold every caret report back; latched holds back selection-only
 * ones; released from waiting drops the press, so nothing is read for it.
 */
export class CaretRule {
	/** The highlight the reader last came to rest in; a caret moving inside it is editing there and does not pick its card again. */
	id: string | null = null;
	private down = false;
	private waiting: object | null = null;
	private latched = false;

	/** The pointer went down. */
	pressStarted() { this.down = true; }
	/** The pointer is up: the press is read once `settled` says it still stands. */
	pressUp(press: object) { this.down = false; this.waiting = press; }
	/** The press's frame came: true when the press still stands (no key or other move released it), so the caller reads it. */
	settled(press: object): boolean {
		if (this.waiting !== press) return false;
		this.waiting = null;
		return true;
	}
	/** The caret was moved by something other than the press: it no longer decides. */
	released() { this.waiting = null; this.latched = false; }
	/** A press is down or not yet read: the editor's own caret reports wait for it. */
	holding(): boolean { return this.down || this.waiting !== null; }
	/** A selection that is not a caret: no highlight holds it. */
	range() { this.id = null; this.latched = false; }

	/** Reads one caret report. Null while the press that decided last still stands; else the highlight found and whether the reader just arrived in it. */
	read<T extends { id: string }>(press: boolean, typed: boolean, find: () => T | undefined): { hit: T | undefined; arrived: boolean } | null {
		if (this.latched && !press) return null;
		this.latched = press;
		const hit = find();
		const arrived = !!hit && hit.id !== this.id && !typed;
		this.id = hit?.id ?? null;
		return { hit, arrived };
	}
}
