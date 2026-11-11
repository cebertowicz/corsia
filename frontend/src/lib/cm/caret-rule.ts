/**
 * Which highlight the reader is in: one rule, one record (`id`). A pointer press decides it, from where the press landed (a phone
 * puts the caret beside the tapped word, not under the finger), and that answer stands until the caret is moved by something other
 * than that press: a key, or a later press. Whatever the browser reports for the same press afterwards, a late 'select' included,
 * never overrides it. Typing at a highlight's edge is editing there, so it is never an arrival.
 */
export class CaretRule {
	/** The highlight the reader last came to rest in; a caret moving inside it is editing there and does not pick its card again. */
	id: string | null = null;
	private pressed = false;

	/** A key was pressed: the caret's own position decides from here on. */
	keyed() { this.pressed = false; }

	/** A selection that is not a caret: no highlight holds it. */
	range() { this.id = null; this.pressed = false; }

	/** Reads one caret report. Null while the press that decided last still stands; else the highlight found and whether the reader just arrived in it. */
	read<T extends { id: string }>(press: boolean, typed: boolean, find: () => T | undefined): { hit: T | undefined; arrived: boolean } | null {
		if (this.pressed && !press) return null;
		this.pressed = press;
		const hit = find();
		const arrived = !!hit && hit.id !== this.id && !typed;
		this.id = hit?.id ?? null;
		return { hit, arrived };
	}
}
