import { Transaction, type EditorState } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { blockRange, cellElement, TABLE, widgetOf } from './widget-blocks';
import { tableCells } from './widget-cells';

/** The cell a press in a table lands on: the one it hit, or, on padding or a border, the one nearest the press point. */
function pressedCell(widget: HTMLElement, event: MouseEvent): Element | null {
	const hit = event.target instanceof Element ? event.target.closest('td, th') : null;
	if (hit && widget.contains(hit)) return hit;
	let nearest: Element | null = null, best = Infinity;
	for (const cell of widget.querySelectorAll('td, th')) {
		const r = cell.getBoundingClientRect();
		const dx = Math.max(r.left - event.clientX, 0, event.clientX - r.right), dy = Math.max(r.top - event.clientY, 0, event.clientY - r.bottom);
		if (dx * dx + dy * dy < best) { best = dx * dx + dy * dy; nearest = cell; }
	}
	return nearest;
}

/** The start of the line after a diagram's closing fence when that line is blank, so typing there makes its own paragraph. */
function blankLineAfter(state: EditorState, to: number): number | null {
	const { doc } = state, n = doc.lineAt(to).number + 1;
	return n <= doc.lines && !doc.line(n).text.trim() ? doc.line(n).from : null;
}

/**
 * A press inside a table or diagram puts the editor's caret where the first keystroke cannot change the block's
 * structure: in a table, the pressed cell's text (the nearest cell's, for a press on padding or a border); for a diagram,
 * the blank line after its fence. Where no such place exists the caret stays where it was. The widget ignores the press
 * and CodeMirror ignores selections inside it, so without this the editor keeps the caret it had before (the start of a
 * document just opened), and the next focus() hands that caret to the keyboard. Labelled a pointer selection, so it picks
 * no comment and releases none; the widget's own click does that.
 */
export function caretToWidget(view: EditorView, event: MouseEvent) {
	const widget = event.target instanceof Element ? widgetOf(view, event.target) : null;
	const block = widget && blockRange(view, widget);
	if (!widget || !block) return;
	const { state } = view, { main } = state.selection;
	let anchor: number | null;
	if (widget.matches(TABLE)) {
		const cell = pressedCell(widget, event);
		const span = cell && tableCells(state.sliceDoc(block.from, block.to)).find((s) => cellElement(widget, s) === cell);
		anchor = span ? Math.min(block.from + span.from, state.doc.length) : null;
	} else anchor = blankLineAfter(state, block.to);
	if (anchor === null || (main.empty && main.head === anchor)) return;
	view.dispatch({ selection: { anchor }, annotations: Transaction.userEvent.of('select.pointer') });
}
