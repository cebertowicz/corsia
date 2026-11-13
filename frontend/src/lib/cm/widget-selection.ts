import { EditorView } from '@codemirror/view';
import { blockRange, cellElement, DIAGRAM } from './widget-blocks';
import { occurrences, snappedTableRows, tableCells, widgetCommentLabel, type CellSpan } from './widget-cells';
import { type WidgetSelection } from './widget-selection-types';

/**
 * What the person selected inside a rendered table or diagram, as a range of the block's own source. The widgets replace
 * their source, so the browser's selection never reaches the editor: this maps it into source offsets and gives the
 * comment flow the same shape it gets for text, with the label and the anchor box the card is later given.
 */

/** Where the Comment offer for selected text sits: above the selection's first line, from where it ends. */
export function textPlace(range: Range): { top: number; left: number } {
	const rects = [...range.getClientRects()];
	const end = rects[rects.length - 1] ?? range.getBoundingClientRect();
	return { top: Math.min(...(rects.length ? rects : [end]).map((r) => r.top)), left: end.right };
}

/** The rows `cells` sit in, across the whole widget: where a table selection's composer is anchored. Null once they are gone. */
export function rowsBox(el: HTMLElement, cells: HTMLElement[]): DOMRect | null {
	const trs = cells.map((cell) => cell.parentElement).filter((tr): tr is HTMLElement => !!tr?.isConnected);
	if (trs.length === 0) return null;
	const top = Math.min(...trs.map((tr) => tr.getBoundingClientRect().top));
	const bottom = Math.max(...trs.map((tr) => tr.getBoundingClientRect().bottom));
	const wide = el.getBoundingClientRect();
	return new DOMRect(wide.left, top, wide.width, bottom - top);
}

/** Maps what the browser has selected inside a widget to source offsets, or null when there is nothing to comment on. */
export function selectionIn(view: EditorView, el: HTMLElement, range: Range): WidgetSelection | null {
	const block = blockRange(view, el);
	if (!block || block.to <= block.from) return null;
	const place = { ...textPlace(range), above: true, inWidget: true as const };
	const { state } = view;
	if (el.matches(DIAGRAM)) {
		return { ...block, text: view.state.sliceDoc(block.from, block.to), ...place, label: () => widgetCommentLabel(state, block.from, block.to, '') ?? undefined, anchor: () => el.isConnected ? el.getBoundingClientRect() : null };
	}
	const src = view.state.sliceDoc(block.from, block.to);
	const origin = range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement;
	const collapsedCell = range.collapsed ? origin?.closest<HTMLTableCellElement>('td, th') ?? null : null;
	const covered = tableCells(src).map((span) => ({ span, cell: cellElement(el, span) }))
		.filter((hit): hit is { span: CellSpan; cell: HTMLTableCellElement } => !!hit.cell && (range.intersectsNode(hit.cell) || hit.cell === collapsedCell))
		.map(({ span, cell }) => {
			// The selection's share of this cell: it starts and ends inside the cell, or at the cell's own edges. A collapsed
		// range at an empty cell's edge does not intersect that element in Chromium, so `collapsedCell` above includes it.
			const part = range.cloneRange(), inCell = document.createRange();
			inCell.selectNodeContents(cell);
			if (range.compareBoundaryPoints(Range.START_TO_START, inCell) < 0) part.setStart(cell, 0);
			if (range.compareBoundaryPoints(Range.END_TO_END, inCell) > 0) part.setEnd(cell, cell.childNodes.length);
			return { span, cell, part, picked: part.toString() };
		})
		.filter(({ span, picked }) => picked.trim() !== '' || (range.collapsed && span.from === span.to));
	if (covered.length === 0) return null;
	const rows = (items: Array<{ cell: HTMLTableCellElement }> = covered) => rowsBox(el, items.map(({ cell }) => cell));
	const first = covered[0], last = covered[covered.length - 1];
	// The label comes from the one shared function, from the same offsets and quote the card is later given.
	const whole = (start: { span: CellSpan; cell: HTMLTableCellElement } = first, end: { span: CellSpan; cell: HTMLTableCellElement } = last, items: Array<{ cell: HTMLTableCellElement }> = covered): WidgetSelection => {
		const from = block.from + start.span.from, to = block.from + end.span.to, text = src.slice(start.span.from, end.span.to);
		return { from, to, text, ...place, label: () => widgetCommentLabel(state, from, to, text) ?? undefined, anchor: () => rows(items) };
	};
	const snapped = snappedTableRows(src, covered.map(({ span }) => span));
	if (snapped && snapped.rows > 1) {
		const all = snapped.cells.map((span) => ({ span, cell: cellElement(el, span) }))
			.filter((hit): hit is { span: CellSpan; cell: HTMLTableCellElement } => !!hit.cell);
		return whole(all[0] ?? first, all[all.length - 1] ?? last, all);
	}
	if (covered.length > 1) return whole();
	// One cell: the occurrence the person selected, by its place among the same text in the rendered cell.
	const { span, cell, part, picked } = first;
	if (range.collapsed && span.from === span.to) {
		const from = block.from + span.anchorFrom, to = block.from + span.anchorTo, text = src.slice(span.anchorFrom, span.anchorTo);
		return { from, to, text, ...place, label: () => widgetCommentLabel(state, from, to, text) ?? undefined, anchor: rows as () => DOMRect | null };
	}
	const rendered = cell.textContent ?? '';
	const before = document.createRange();
	before.selectNodeContents(cell);
	before.setEnd(part.startContainer, part.startOffset);
	const index = occurrences(rendered, picked).filter((at) => at < before.toString().length).length;
	const inSource = occurrences(src.slice(span.from, span.to), picked);
	if (inSource.length === occurrences(rendered, picked).length && inSource[index] !== undefined) {
		const from = block.from + span.from + inSource[index];
		return { from, to: from + picked.length, text: picked, ...place, label: () => widgetCommentLabel(state, from, from + picked.length, picked) ?? undefined, anchor: rows as () => DOMRect | null };
	}
	return whole();
}
