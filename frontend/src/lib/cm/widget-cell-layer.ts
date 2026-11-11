import type { EditorView } from '@codemirror/view';
import { clean } from './widget-cells';

/** Marks the cell whose text is stepped aside while its editor lies over it. */
export const CELL_EDITING = 'cm-md-cell-editing';

/**
 * What a screen reader calls the open cell's editor: its column's header and its body row, counted from 1, or the column's
 * number when its header is empty. A header cell has no row of its own to name, so it is named as its column's header.
 */
export function cellLabel(td: HTMLTableCellElement): string {
	const row = td.parentElement instanceof HTMLTableRowElement ? td.parentElement.rowIndex : 0, col = td.cellIndex + 1;
	if (row === 0) return `Column ${col} header`;
	const header = clean(td.closest('table')?.rows[0]?.cells[td.cellIndex]?.textContent ?? '');
	return `${header || `Column ${col}`}, row ${row}`;
}

/** Every element around `cell` that cuts off what overflows it: the table's sideways scroll, the document's scroll area, the pane. */
function clippersOf(cell: Element): Element[] {
	const found: Element[] = [];
	for (let el = cell.parentElement; el && el !== document.body; el = el.parentElement) {
		const style = getComputedStyle(el);
		if (style.overflowX !== 'visible' || style.overflowY !== 'visible') found.push(el);
	}
	return found;
}

/**
 * The part of a layer at `box`, `height` tall, that shows inside every one of `clippers`, as the clip-path that cuts the rest
 * away; '' when all of it shows. The layer is fixed to the window, outside them all, so nothing else cuts it off as it
 * does the cell under it.
 */
export function clipTo(box: DOMRect, height: number, clippers: Element[]): string {
	let top = box.top, left = box.left, right = box.right, bottom = box.top + height;
	for (const el of clippers) {
		const outer = el.getBoundingClientRect(), x = outer.left + el.clientLeft, y = outer.top + el.clientTop;
		top = Math.max(top, y);
		left = Math.max(left, x);
		right = Math.min(right, x + el.clientWidth);
		bottom = Math.min(bottom, y + el.clientHeight);
	}
	const cut = [top - box.top, box.right - right, box.top + height - bottom, left - box.left].map((side) => Math.max(0, side));
	return cut.some((side) => side > 0) ? `inset(${cut.map((side) => `${side}px`).join(' ')})` : '';
}

/**
 * How far in from the cell's box its text starts on `side`: its padding, and in a table whose borders are collapsed, the
 * share of the border that lies inside the cell. The editor draws no border, so without that share its text sits that far out.
 * On the left the share is read off where the cell lays its own content (a border is snapped to the screen's pixels, so half
 * its computed width is not what is drawn); a share that reads as more than a pixel is something other than a border.
 */
function inset(cell: HTMLTableCellElement, style: CSSStyleDeclaration, side: 'Top' | 'Right' | 'Bottom' | 'Left', box: DOMRect): string {
	const padding = style[`padding${side}`];
	const table = cell.closest('table');
	if (!table || getComputedStyle(table).borderCollapse !== 'collapse') return padding;
	if (side === 'Left') {
		const range = document.createRange();
		range.selectNodeContents(cell);
		const lefts = [...range.getClientRects()].filter((r) => r.width > 0).map((r) => r.left);
		const share = lefts.length ? Math.min(...lefts) - box.left - parseFloat(padding) : NaN;
		if (share >= 0 && share <= 1) return `calc(${padding} + ${share}px)`;
	}
	return `calc(${padding} + ${parseFloat(style[`border${side}Width`]) / 2}px)`;
}

/**
 * Where a cell's editor is allowed to live.
 *
 * A rendered cell is inside CodeMirror's own editing host, and Chromium will not put a caret in a contenteditable element
 * inside another one: measured in the e2e harness, a `contenteditable` island outside the editor takes focus and takes text,
 * the same island in the open cell takes neither, with the widget's own editing flag raised, lowered, or the outer editor's
 * editing turned off. So the editor is held in a layer that sits in the editor's own box but outside its editing host, which
 * is where it inherits the document's fonts and theme, and its box is kept on the cell it edits. The cell keeps its live text
 * underneath, unseen, so the row still grows as that text wraps and the table never jumps; the table itself is still drawn
 * the one way it has always been drawn.
 */
export class CellLayer {
	/** The element the cell's editor is placed in, reused from one cell to the next. */
	readonly el = document.createElement('div');
	private cell: HTMLTableCellElement | null = null;
	private clippers: Element[] = [];
	private frame = 0;
	/** What was last written to the layer, so a frame where nothing moved writes nothing. */
	private written = '';

	constructor() { this.el.className = 'cm-md-cell-layer'; }

	/** Puts `editor` over `td` and follows that cell's box until the layer closes. */
	open(td: HTMLTableCellElement, editor: HTMLElement, host: EditorView) {
		this.close();
		this.cell = td;
		this.clippers = clippersOf(td);
		td.classList.add(CELL_EDITING);
		this.el.append(editor);
		host.dom.append(this.el);
		this.follow();
		// The cell's box moves when its own text rewraps, when the table is redrawn under a remote edit, when the document
		// scrolls or the window resizes, and when something drawn above the editor (a notice, a bar) comes or goes and moves
		// the whole editor down the page with no resize or scroll to announce it. So the layer reads the cell's box every
		// frame while it is open, and writes only when that box has changed.
		const tick = () => { this.follow(); this.frame = requestAnimationFrame(tick); };
		this.frame = requestAnimationFrame(tick);
	}

	/** The click that closes the cell: anything outside the editor is elsewhere. */
	contains = (node: Node | null) => this.el.contains(node);

	/**
	 * Copies the cell's box and padding, so the text in the editor wraps exactly as it does in the cell. Also called after
	 * each document change, since an edit above the table moves the cell without resizing anything the layer observes.
	 */
	readonly follow = () => {
		const cell = this.cell, el = this.el;
		if (!cell?.isConnected) return;
		const box = cell.getBoundingClientRect(), style = getComputedStyle(cell);
		// The cell's own type, not the editor's: a line that wraps at a different width, or at a different height, would put
		// the reader's text out of step with the row they are editing. The renderer wraps a cell's text in an element of its
		// own, and that element carries the type, so it is asked rather than the cell around it.
		const face = cell.querySelector<HTMLElement>('p, div, li, span') ?? cell;
		const type = face === cell ? style : getComputedStyle(face);
		const next: Partial<Record<keyof CSSStyleDeclaration & string, string>> = {
			left: `${box.left}px`, top: `${box.top}px`, width: `${box.width}px`, minHeight: `${box.height}px`,
			padding: `${inset(cell, style, 'Top', box)} ${inset(cell, style, 'Right', box)} ${inset(cell, style, 'Bottom', box)} ${inset(cell, style, 'Left', box)}`,
			// A row is as tall as its tallest cell, and a shorter cell's text sits where its vertical alignment puts it; the
			// editor's text sits there too, so opening a cell in a tall row does not move its text to the top.
			justifyContent: style.verticalAlign === 'middle' ? 'center' : style.verticalAlign === 'bottom' ? 'flex-end' : 'flex-start',
			// A cell scrolled partly out of the table's sideways scroll or the document's scroll area is cut off there; so is its editor.
			clipPath: clipTo(box, Math.max(box.height, el.offsetHeight), this.clippers),
		};
		for (const property of ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'overflowWrap', 'wordBreak'] as const) next[property] = type[property];
		const signature = JSON.stringify(next);
		if (signature === this.written) return;
		this.written = signature;
		Object.assign(el.style, next);
	};

	/** Takes the layer out of the document and leaves the cell as a plain rendered cell again. */
	close() {
		this.cell?.classList.remove(CELL_EDITING);
		this.cell = null;
		this.clippers = [];
		cancelAnimationFrame(this.frame);
		this.frame = 0;
		this.written = '';
		this.el.remove();
	}
}
