import { Transaction, type Text } from '@codemirror/state';

/**
 * What a cell's editor accepts, which is what cannot break the table it sits in: a pipe that would end the cell is stored
 * escaped, a line break has nowhere to go, and half an escape pair would leave a bare pipe behind.
 */

/** The smallest change from one cell text to the next, so the caret the reader placed maps through it instead of jumping to an end. */
export function cellDifference(oldText: string, newText: string) {
	let keep = 0;
	while (keep < oldText.length && keep < newText.length && oldText[keep] === newText[keep]) keep++;
	let tail = 0;
	while (tail < oldText.length - keep && tail < newText.length - keep && oldText[oldText.length - 1 - tail] === newText[newText.length - 1 - tail]) tail++;
	return { from: keep, to: oldText.length - tail, insert: newText.slice(keep, newText.length - tail) };
}

const ONLY_BREAKS = /^\n+$/;

/**
 * Escapes each `|` in `text` only when the backslashes before it run even, counting the ones the cell already holds
 * before `at`. An even run means the new pipe is not escaped by what precedes it — a `\` typed a keystroke earlier, or a
 * pasted `\\` — and an odd run means it already is, so nothing is added and no escape is doubled.
 */
export function escapeCellPipes(doc: Text, at: number, text: string): string {
	let run = 0;
	while (doc.sliceString(at - run - 1, at - run) === '\\') run++;
	let out = '';
	for (const char of text) {
		if (char === '\\') { run++; out += char; continue; }
		if (char === '|') { out += run % 2 === 0 ? '\\|' : '|'; run = 0; continue; }
		run = 0;
		out += char;
	}
	return out;
}

/**
 * The rewrite itself: every inserted pipe leaves an odd run of backslashes behind it, a paste of a block of lines becomes
 * one line, Enter on its own does nothing, and a deletion that would leave half of an escape pair takes the pair, since a
 * bare `|` ends the cell and splits the row.
 */
export function cellShapeOnly(transaction: Transaction) {
	const doc = transaction.startState.doc;
	const changes: { from: number; to: number; insert: string }[] = [];
	let rewritten = false;
	transaction.changes.iterChanges((fromA, toA, _fromB, _toB, insert) => {
		let from = fromA, to = toA;
		const before = insert.sliceString(0, insert.length);
		let text = before;
		if (from !== to) {
			if (doc.sliceString(to - 1, to) === '\\' && doc.sliceString(to, to + 1) === '|') to++;
			if (doc.sliceString(from - 1, from) === '\\' && doc.sliceString(from, from + 1) === '|') from--;
		}
		if (text.includes('\n')) text = ONLY_BREAKS.test(text) ? '' : text.replace(/\n+/g, ' ');
		if (text.includes('|')) text = escapeCellPipes(doc, from, text);
		rewritten ||= from !== fromA || to !== toA || text !== before;
		changes.push({ from, to, insert: text });
	});
	if (!rewritten) return transaction;
	// The caret follows the rewritten change rather than a position computed for the text the reader typed.
	const userEvent = transaction.annotation(Transaction.userEvent);
	return [userEvent ? { changes, userEvent } : { changes }];
}
