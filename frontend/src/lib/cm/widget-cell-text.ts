import { EditorSelection, Transaction, type Text } from '@codemirror/state';
import { ViewPlugin, type EditorView, type ViewUpdate } from '@codemirror/view';
import { runDifference } from './widget-cell-row';

/** How a cell's editor holds its content: the pads around the text, and the caret kept on the text. */

/**
 * The smallest changes from one cell text to the next, so the caret the reader placed maps through them instead of jumping
 * to an end. When only the padding around the same text differs (the cell's pipes were written around what was typed into
 * a padded cell), the pads change and the text between them is left alone, so a caret in it stays after the character it
 * was after and the next key lands there, not past the trailing pad.
 */
export function cellDifference(oldText: string, newText: string) {
	const oldCore = oldText.trim();
	if (oldCore && oldCore === newText.trim()) {
		const oldLead = oldText.length - oldText.trimStart().length, newLead = newText.length - newText.trimStart().length;
		return [
			runDifference(oldText.slice(0, oldLead), newText.slice(0, newLead)),
			runDifference(oldText.slice(oldLead + oldCore.length), newText.slice(newLead + oldCore.length), oldLead + oldCore.length)
		].filter((change) => change.from !== change.to || change.insert);
	}
	return [runDifference(oldText, newText)];
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
