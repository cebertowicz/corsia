import { EditorView } from '@codemirror/view';
import { blockRange, cellElement, inWidgetSource, TABLE, widgetBoxAt } from './widget-blocks';
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
 * Scrolls to a comment inside a table or diagram. CodeMirror's own scroll targets the replaced source, which has no box of its
 * own, so it lands on the widget's edge: this brings the widget on screen, then centres its row (or the diagram).
 * Returns false for a position outside any widget, which the caller scrolls as usual.
 */
export function scrollToWidget(view: EditorView, pos: number, band: () => { top: number; bottom: number }, top: boolean): boolean {
	if (!inWidgetSource(view, pos)) return false;
	// CodeMirror centres a position inside a widget on the whole replaced block, which can be thousands of pixels tall. A
	// widget already drawn is placed by its row below; dispatching to it would jump the scroller to the block's middle for
	// a frame, and CodeMirror's measure in that frame drops the lines above from the document.
	if (!widgetBoxAt(view, pos)) view.dispatch({ effects: EditorView.scrollIntoView(pos, { y: 'center' }) });
	const settle = (pass: number) => requestAnimationFrame(() => {
		if (!view.dom.isConnected || pass > 12) return;
		revealWidgetCell(view, pos);
		const box = widgetBoxAt(view, pos), visible = band();
		if (!box) { view.dispatch({ effects: EditorView.scrollIntoView(pos, { y: 'center' }) }); return void settle(pass + 1); }
		const want = top ? visible.top + box.height : (visible.top + visible.bottom) / 2;
		if (Math.abs(box.top + box.height / 2 - want) > box.height) view.scrollDOM.scrollTop += box.top + box.height / 2 - want;
		settle(pass + 1);
	});
	settle(0);
	return true;
}
