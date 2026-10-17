import type { EditorView } from '@codemirror/view';
import { blockRange, cellElement, TABLE, widgetBoxAt, widgetSourceAt } from './widget-blocks';
import { tableCells } from './widget-cells';

/** A narrow table scrolls horizontally inside its widget; a jump to a cell makes that cell visible as well as its row. */
function revealWidgetCell(view: EditorView, pos: number) {
	for (const el of view.contentDOM.querySelectorAll<HTMLElement>(TABLE)) {
		const block = blockRange(view, el);
		if (!block || pos < block.from || pos > block.to) continue;
		const src = view.state.sliceDoc(block.from, block.to);
		const span = tableCells(src).find((cell) => cell.from <= pos - block.from && pos - block.from <= cell.to);
		const cell = span && cellElement(el, span), wrap = cell?.closest<HTMLElement>('.table-wrap');
		if (!cell || !wrap) return;
		const target = cell.getBoundingClientRect(), visible = wrap.getBoundingClientRect();
		if (target.left < visible.left || target.right > visible.right) wrap.scrollLeft += target.left + target.width / 2 - (visible.left + visible.width / 2);
		return;
	}
}

/**
 * Where a comment inside a table or diagram sits. CodeMirror's own scroll targets the replaced source, which has no box of its
 * own, so `scrollToRange` brings the widget's start into view and, once the widget is drawn, places its row (or the diagram)
 * itself. `box` is that row now, null while the widget is not drawn; `reveal` scrolls a narrow table to the cell.
 */
export interface WidgetTarget {
	start: number;
	box: (view: EditorView) => DOMRect | null;
	reveal: (view: EditorView) => void;
}

/** The widget target for a position, or null outside any table or diagram, which CodeMirror scrolls as usual. */
export function widgetTarget(view: EditorView, pos: number): WidgetTarget | null {
	const source = widgetSourceAt(view, pos);
	return source && { start: source.from, box: (current) => widgetBoxAt(current, pos), reveal: (current) => revealWidgetCell(current, pos) };
}
