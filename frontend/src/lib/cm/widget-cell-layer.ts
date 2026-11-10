import type { EditorView } from '@codemirror/view';

/** Marks the cell whose text is stepped aside while its editor lies over it. */
export const CELL_EDITING = 'cm-md-cell-editing';

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
	private tracked: ResizeObserver | undefined;

	constructor() { this.el.className = 'cm-md-cell-layer'; }

	/** Puts `editor` over `td` and follows that cell's box until the layer closes. */
	open(td: HTMLTableCellElement, editor: HTMLElement, host: EditorView) {
		this.close();
		this.cell = td;
		td.classList.add(CELL_EDITING);
		this.el.append(editor);
		host.dom.append(this.el);
		this.follow();
		// The cell's box moves when its own text rewraps, when the table is redrawn under a remote edit, when the panel or
		// sidebar changes the document's width, and when the document or the page scrolls or the window resizes.
		this.tracked = typeof ResizeObserver === 'function' ? new ResizeObserver(this.follow) : undefined;
		this.tracked?.observe(td);
		this.tracked?.observe(host.dom);
		window.addEventListener('scroll', this.follow, { capture: true, passive: true });
		window.addEventListener('resize', this.follow);
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
		el.style.left = `${box.left}px`;
		el.style.top = `${box.top}px`;
		el.style.width = `${box.width}px`;
		el.style.minHeight = `${box.height}px`;
		el.style.padding = `${style.paddingTop} ${style.paddingRight} ${style.paddingBottom} ${style.paddingLeft}`;
		// The cell's own type, not the editor's: a line that wraps at a different width, or at a different height, would put
		// the reader's text out of step with the row they are editing. The renderer wraps a cell's text in an element of its
		// own, and that element carries the type, so it is asked rather than the cell around it.
		const face = cell.querySelector<HTMLElement>('p, div, li, span') ?? cell;
		const type = face === cell ? style : getComputedStyle(face);
		for (const property of ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'overflowWrap', 'wordBreak'] as const) {
			el.style[property] = type[property];
		}
	};

	/** Takes the layer out of the document and leaves the cell as a plain rendered cell again. */
	close() {
		this.cell?.classList.remove(CELL_EDITING);
		this.cell = null;
		this.tracked?.disconnect();
		this.tracked = undefined;
		this.el.remove();
		window.removeEventListener('scroll', this.follow, { capture: true });
		window.removeEventListener('resize', this.follow);
	}
}
