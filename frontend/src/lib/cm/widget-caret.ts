import { Transaction } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { blockRange, cellElement, TABLE, widgetOf } from './widget-blocks';
import { tableCells } from './widget-cells';

/**
 * A press inside a table or diagram puts the editor's caret on that place in the source: the clicked cell's text, or the
 * diagram's fence. The widget ignores the press and CodeMirror ignores selections inside it, so without this the editor
 * keeps the caret it had before (the start of a document just opened), and the next focus() hands that caret to the
 * keyboard. Labelled a pointer selection, so it picks no comment and releases none; the widget's own click does that.
 */
export function caretToWidget(view: EditorView, target: EventTarget | null) {
	const widget = target instanceof Element ? widgetOf(view, target) : null;
	const block = widget && blockRange(view, widget);
	if (!widget || !block) return;
	const cell = widget.matches(TABLE) ? (target as Element).closest('td, th') : null;
	const span = cell ? tableCells(view.state.sliceDoc(block.from, block.to)).find((s) => cellElement(widget, s) === cell) : undefined;
	const anchor = Math.min(block.from + (span?.from ?? 0), view.state.doc.length);
	if (view.state.selection.main.empty && view.state.selection.main.head === anchor) return;
	view.dispatch({ selection: { anchor }, annotations: Transaction.userEvent.of('select.pointer') });
}
