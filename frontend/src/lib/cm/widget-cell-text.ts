import { ChangeSet, EditorSelection, Transaction, type Text } from '@codemirror/state';
import { ViewPlugin, type EditorView, type ViewUpdate } from '@codemirror/view';

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

/** Whether `at` sits between an escape's backslash and its pipe, a place the reader cannot see, since the backslash is hidden. */
const insideEscape = (doc: Text, at: number) => doc.sliceString(at - 1, at) === '\\' && doc.sliceString(at, at + 1) === '|';

/**
 * Where a backslash has to go so that every `|` in the cell's text has an odd run of backslashes before it again, given
 * the ranges the change put in. A pipe the change put in is escaped in place; a pipe the change left behind an even run —
 * a backslash typed or left by a deletion right before an escape pair — gets its backslash where the change ended, so
 * that backslash becomes a literal one and the caret after it still sits before the pair.
 */
function unescapedPipes(text: string, inserted: { from: number; to: number }[]) {
	const at: number[] = [];
	let run = 0;
	for (let pipe = 0; pipe < text.length; pipe++) {
		if (text[pipe] === '\\') { run++; continue; }
		if (text[pipe] === '|' && run % 2 === 0) {
			const typed = inserted.some((range) => range.from <= pipe && pipe < range.to);
			const ended = typed ? undefined : inserted.findLast((range) => pipe - run <= range.to && range.to <= pipe);
			at.push(ended ? ended.to : pipe);
		}
		run = 0;
	}
	return at;
}

/**
 * The rewrite itself: a paste of a block of lines becomes one line, Enter on its own does nothing, a deletion that would
 * leave half of an escape pair takes the pair, and a character typed between an escape's hidden backslash and its pipe
 * goes in front of the pair. Then every `|` in the cell's resulting text is checked, not only the ones typed, because a
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
			if (insideEscape(doc, to)) to++;
			if (insideEscape(doc, from)) from--;
		} else if (insideEscape(doc, from)) from = to = from - 1;
		if (text.includes('\n')) text = ONLY_BREAKS.test(text) ? '' : text.replace(/\n+/g, ' ');
		rewritten ||= from !== fromA || to !== toA || text !== before;
		changes.push({ from, to, insert: text });
	});
	let applied = ChangeSet.of(changes, doc.length);
	const inserted: { from: number; to: number }[] = [];
	applied.iterChangedRanges((_fromA, _toA, from, to) => inserted.push({ from, to }));
	const escapes = unescapedPipes(applied.apply(doc).toString(), inserted);
	if (escapes.length) applied = applied.compose(ChangeSet.of(escapes.map((at) => ({ from: at, insert: '\\' })), applied.newLength));
	if (!rewritten && !escapes.length) return transaction;
	const userEvent = transaction.annotation(Transaction.userEvent);
	// The caret follows the text that actually went in, which is longer than what the reader typed once an escape was added:
	// mapped through the original change it would sit before the escaped pipe, and the next keystroke would land there.
	// So the caret the transaction asked for is taken back to where it stood before the change, then carried past whatever
	// the rewritten change put in at that place.
	const back = transaction.changes.invertedDesc;
	const asked = transaction.selection;
	const heads = asked ? asked.ranges.map((range) => back.mapPos(range.head, -1)) : transaction.startState.selection.ranges.map((range) => range.head);
	const main = asked ? asked.mainIndex : transaction.startState.selection.mainIndex;
	const selection = EditorSelection.create(heads.map((head) => EditorSelection.cursor(applied.mapPos(head, 1))), main);
	return [userEvent ? { changes: applied, userEvent, selection } : { changes: applied, selection }];
}

/**
 * Where a cell's own text starts and ends inside the slice its editor holds, which includes the spaces padding it from its
 * pipes. Those spaces are not drawn, so a caret among them would type a space the reader never sees; an all-blank cell's
 * text is its middle, so what is typed into it sits between both pads.
 */
export function cellTextBounds(doc: Text) {
	const text = doc.toString();
	const lead = text.length - text.trimStart().length;
	if (lead === text.length) return { from: Math.min(1, text.length), to: Math.min(1, text.length) };
	return { from: lead, to: text.trimEnd().length };
}

/**
 * The spaces padding a cell's text from its pipes are table syntax the cell does not draw, so they must not move the text.
 * They stay in the editor as real text, since a space typed at either edge of the text has to land in the source and the
 * caret has to sit beside them as it sits beside any text; hiding them would leave the caret against a hidden range. The
 * trailing pad hangs past the line's end (`pre-wrap`, in the theme), and the leading pad, always on the first line, is
 * pulled back by a negative indent of its own drawn width, so the text starts and wraps where the cell draws it.
 */
export const cellPaddingFlush = ViewPlugin.fromClass(class {
	constructor(view: EditorView) { this.measure(view); }

	update(update: ViewUpdate) {
		if (update.docChanged || update.geometryChanged) this.measure(update.view);
	}

	measure(view: EditorView) {
		view.requestMeasure({
			read: (view) => {
				const lead = cellTextBounds(view.state.doc).from;
				const start = lead ? view.coordsAtPos(0, 1) : null, text = lead ? view.coordsAtPos(lead, -1) : null;
				return start && text ? Math.max(0, text.left - start.left) : 0;
			},
			write: (width, view) => { view.contentDOM.style.textIndent = width ? `-${width}px` : ''; }
		});
	}
});

/**
 * Keeps a caret that moved without typing (a click, an arrow, Home or End) on the cell's drawn text, so the keys put it
 * where they would in prose: End after the last character the reader sees, Home before the first.
 */
export function caretOnCellText(transaction: Transaction) {
	const selection = transaction.selection;
	if (transaction.docChanged || !selection) return transaction;
	const { from, to } = cellTextBounds(transaction.newDoc);
	const onText = (at: number) => Math.min(Math.max(at, from), to);
	if (selection.ranges.every((range) => onText(range.anchor) === range.anchor && onText(range.head) === range.head)) return transaction;
	const userEvent = transaction.annotation(Transaction.userEvent);
	return {
		selection: EditorSelection.create(selection.ranges.map((range) => EditorSelection.range(onText(range.anchor), onText(range.head))), selection.mainIndex),
		effects: transaction.effects,
		scrollIntoView: transaction.scrollIntoView,
		annotations: userEvent ? [Transaction.userEvent.of(userEvent)] : []
	};
}
