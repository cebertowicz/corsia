import { markdown } from '@codemirror/lang-markdown';
import { EditorState, Facet, Transaction, type Extension } from '@codemirror/state';
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { GFM } from '@lezer/markdown';
import { emitProductEvent } from '$lib/product_events';
import { blockRange, cellElement, drawnTableAt, TABLE } from './widget-blocks';
import { cellClicked } from './widget-cell-click';
import {
	CELL_SYNC, cellSource, cellToOpen, closeCell, openCellAt, openCellInSource, openCellState, type OpenCell
} from './widget-cell-state';
import { caretOnCellText, cellDifference, cellPaddingFlush, cellShapeOnly, inTableCell } from './widget-cell-text';
import { adjacentCell, caretAfterTable, cellBelow, tableOfCell } from './widget-cell-nav';
import { cellKeys } from './widget-cell-keys';
import { CellSwipe } from './widget-cell-swipe';
import { CELL_THEME } from './widget-cell-theme';
import { CellLayer, cellLabel } from './widget-cell-layer';
import { repaintTableCell } from './widget-table';
import { cellHighlight } from './theme';

/**
 * Editing the text of a rendered table cell where it is read.
 *
 * Clicking a drawn cell opens a small editor over that cell's own `<td>`, holding exactly that cell's source slice
 * between its pipes. So one keystroke is one transaction against the cell's own range, and the document keeps its single
 * Yjs binding, its single undo manager and its single markdown parse: the nested editor holds no document state of its
 * own and is rewritten from the document whenever the two disagree, which is also how a live edit from another client
 * reaches the cell. Formatting renders while you type because the cell runs the document's own hide-syntax pass, handed
 * to it through `cellEditorExtensions`.
 *
 * The editor sits in `widget-cell-layer.ts`'s layer rather than in the `<td>` itself, because a caret cannot be placed
 * inside the document's own editing host; the cell keeps its live rendered text underneath, unseen, so the row keeps
 * growing as the text wraps. The cell is repainted from the source when the editor closes, and the table's DOM survives
 * the whole edit because `widget-table.ts` keeps it, and this `<td>`, alive across edits.
 */

/** The extensions a cell's own editor runs over its source. `markdown-live.ts` supplies the document's hide-syntax pass. */
export const cellEditorExtensions = Facet.define<Extension, readonly Extension[]>({ combine: (values) => values });

type Change = { from: number; to: number; insert: string };

/** The cell's editor, and the cell it is held over, for as long as one cell is open. */
class CellEditors {
	private cell: OpenCell | null = null;
	private left: OpenCell | null = null;
	private fresh = false;
	// Whether the reader has typed in the open cell, and the text it held when it opened: leaving it with other text is an edit.
	private edited = false;
	private opened = '';
	private editor: EditorView | null = null;
	private wrap: HTMLElement | null = null;
	private mounted: HTMLTableCellElement | null = null;
	private readonly layer = new CellLayer();
	// Where the click that opened a cell landed, so the caret goes there and not to the start of the cell's text.
	private clickedAt: { x: number; y: number } | null = null;
	private readonly swipe: CellSwipe;

	constructor(private readonly host: EditorView) {
		this.swipe = new CellSwipe();
		host.dom.addEventListener('click', this.openPressed);
		document.addEventListener('pointerdown', this.pressedElsewhere, true);
	}

	/** A click on a drawn cell edits it: a caret in that cell, the way a click in prose places one. */
	private readonly openPressed = (event: MouseEvent) => {
		const host = this.host;
		if (this.swipe.swiped()) return;
		const clicked = cellClicked(host, event.target instanceof Element ? event.target : null, { x: event.clientX, y: event.clientY });
		if (!clicked) return;
		const { widget, cell, row } = clicked;
		const block = blockRange(host, widget);
		if (!block) return;
		const open = cellToOpen(host.state, block.from, row.rowIndex, cell.cellIndex);
		if (open) {
			this.clickedAt = { x: event.clientX, y: event.clientY };
			host.dispatch({ effects: openCellAt.of(open) });
		}
	};

	/** A press outside the open cell closes it. A touch closes it only if it ends as a tap, the rule that opens a cell: a swipe is a scroll. */
	private readonly pressedElsewhere = (press: PointerEvent) => {
		if (!this.cell || this.layer.contains(press.target as Node)) return;
		if (!this.swipe.whenTapped(press, () => { if (this.cell) this.close(); })) this.close();
	};

	/**
	 * Called after the host view has finished applying a change, which is the first moment a nested editor may be created or
	 * sent a change: while the host is mid-update, CodeMirror refuses both. So this is where the cell's editor is opened,
	 * moved, synced to the document and closed.
	 */
	settle(update: ViewUpdate) {
		const cell = update.state.field(openCellState);
		update.view.dom.classList.toggle('cm-md-cell-open', !!cell);
		if (!cell) { this.detach(); return; }
		// A document that turns read-only (while a suggestion is applied, say) takes no more typing: the cell closes, keeping
		// what was already typed, since every keystroke before this one is in the document.
		if (update.state.readOnly) { this.close(); return; }
		const was = this.cell;
		const another = !was || was.row !== cell.row || was.col !== cell.col;
		const moved = another || was.from !== cell.from || was.to !== cell.to || was.tableFrom !== cell.tableFrom;
		this.cell = cell;
		if (another && was && this.editor) {
			// The cell the editor is leaving goes back to being rendered text before the editor moves on to the next one.
			const previous = drawnTableAt(this.host, was.tableFrom);
			if (previous) repaintTableCell(previous, update.state, was);
			this.committed();
			this.left = was;
			this.mounted = null;
		}
		if (!this.editor) this.attach(cell);
		const editor = this.editor;
		if (!editor) return;
		if (!this.mount(cell)) return;
		if (moved || update.docChanged) {
			const text = cellSource(update.state, cell), current = editor.state.doc.toString();
			if (current !== text) editor.dispatch({ changes: cellDifference(current, text), userEvent: CELL_SYNC });
		}
		if (another) this.opened = editor.state.doc.toString().trim();
		if (moved) this.mirrorToDocument(cell, editor);
	}

	/** Puts the editor over the cell it edits. A cell the table does not draw closes: with no cell to lie over, nothing could show the caret. False when it closed. */
	private mount(cell: OpenCell): boolean {
		const wrap = this.wrap;
		if (!wrap) return true;
		// Looked up by the table's first character, not the cell's: an empty cell's range sits on a delimiter the renderer
		// does not draw, so its own position can resolve to the node after the table.
		const widget = drawnTableAt(this.host, cell.tableFrom);
		const td = widget ? cellElement(widget, cell) : null;
		if (!td) { this.close(); return false; }
		if (td === this.mounted && this.layer.contains(wrap)) { this.clickedAt = null; this.layer.follow(); return true; }
		const hadFocus = wrap.contains(document.activeElement);
		this.layer.open(td, wrap, this.host);
		this.mounted = td;
		// Named for the cell it is over, so a screen reader says which cell is being edited, and again after Tab or Enter.
		this.editor?.contentDOM.setAttribute('aria-label', cellLabel(td));
		// Focus is taken only once the editor is in the document — before that, focusing it does nothing — and the click
		// that opened the cell counts as the reader asking for the caret there, at the word they clicked rather than at the
		// start of the cell. The layer is over the cell by now, so the editor reads the click's point off its own layout.
		if (hadFocus || this.fresh) {
			this.editor?.focus();
			this.fresh = false;
			this.placeAtClick();
		}
		this.editor?.requestMeasure();
		return true;
	}

	/** The caret where the click landed, as a click in prose puts it. */
	private placeAtClick() {
		const clicked = this.clickedAt, editor = this.editor;
		this.clickedAt = null;
		if (!clicked || !editor) return;
		// The nearest place in the cell's text, so a press on padding or a border still leaves the caret where typing lands.
		const at = editor.posAtCoords(clicked, false);
		editor.dispatch({ selection: { anchor: at, head: at } });
	}

	/**
	 * The reader's place in the cell, as a range of the document, so the comment, suggestion and highlight flows see a
	 * selection inside a cell as the same kind of selection they already handle in prose.
	 */
	private mirrorToDocument(cell: OpenCell, editor: EditorView) {
		const { anchor, head } = editor.state.selection.main;
		const selection = this.host.state.selection.main;
		if (selection.from === cell.from + anchor && selection.to === cell.from + head) return;
		this.host.dispatch({ selection: { anchor: cell.from + anchor, head: cell.from + head }, userEvent: CELL_SYNC });
	}

	private close() {
		this.clickedAt = null;
		this.left = this.cell;
		this.cell = null;
		if (this.host.state.field(openCellState)) this.host.dispatch({ effects: closeCell.of(null) });
	}

	/** Leaves the table for good: the caret goes below it, where the reader was heading. */
	private leave() {
		const block = this.cell ? tableOfCell(this.host.state, this.cell) : null;
		const at = block ? caretAfterTable(this.host.state, block.to) : this.host.state.doc.length;
		this.left = this.cell;
		this.cell = null;
		this.host.dispatch({ effects: closeCell.of(null), selection: { anchor: at, head: at }, scrollIntoView: true });
		// The cell's editor held the focus and is gone now, so the document takes it back for the caret placed below the table.
		this.host.focus();
	}

	private moveTo(next: OpenCell | null) {
		if (!next) { this.leave(); return; }
		this.left = this.cell;
		this.host.dispatch({ effects: openCellAt.of(next) });
		// The caret goes to the end of the next cell's text: the end of its slice, which the caret filter keeps on the text.
		this.editor?.dispatch({ selection: { anchor: this.editor.state.doc.length } });
		this.editor?.focus();
	}

	/** The cell's own editor: the document's markdown parse and hide-syntax pass, and no history of its own. */
	private attach(cell: OpenCell) {
		const host = this.host;
		// The caret goes where the click landed: the document's selection is the click's position, mapped into the cell.
		const clicked = host.state.selection.main;
		const at = clicked.from >= cell.from && clicked.from <= cell.to ? clicked.from - cell.from : 0;
		// The element a cell's editor is recognised by: the comment flows and the paint pass leave it alone.
		const wrap = document.createElement('div');
		wrap.className = 'cm-md-cell-editor';
		this.wrap = wrap;
		this.fresh = true;
		this.editor = new EditorView({
			parent: wrap,
			state: EditorState.create({
				doc: cellSource(host.state, cell),
				selection: { anchor: at, head: at },
				extensions: [
					markdown({ extensions: GFM }),
					...host.state.facet(cellEditorExtensions),
					inTableCell.of(true),
					cellHighlight,
					EditorView.lineWrapping,
					cellPaddingFlush,
					EditorState.transactionFilter.of((tr) => (tr.isUserEvent(CELL_SYNC) ? tr : tr.docChanged ? cellShapeOnly(tr) : caretOnCellText(tr))),
					EditorView.updateListener.of(this.fromCell),
					cellKeys(host, {
						leave: () => this.leave(),
						next: () => this.moveTo(adjacentCell(host.state, this.cell!, 1)),
						previous: () => this.moveTo(adjacentCell(host.state, this.cell!, -1)),
						below: () => this.moveTo(cellBelow(host.state, this.cell!))
					}),
					CELL_THEME
				]
			})
		});
	}

	/** What the cell's editor did: send its change to the document on the cell's own range, and its selection as a document range. */
	private readonly fromCell = (update: ViewUpdate) => {
		const cell = this.cell, editor = this.editor;
		if (!cell || !editor) return;
		if (update.docChanged && !update.transactions.every((tr) => tr.isUserEvent(CELL_SYNC))) {
			const changes: Change[] = [];
			update.changes.iterChanges((fromA, toA, _fromB, _toB, insert) => {
				changes.push({ from: cell.from + fromA, to: cell.from + toA, insert: insert.sliceString(0, insert.length) });
			});
			if (cell.padded) openCellInSource(this.host.state, changes, cell);
			const event = update.transactions[update.transactions.length - 1]?.annotation(Transaction.userEvent);
			this.edited = true;
			this.host.dispatch(event ? { changes, userEvent: event } : { changes });
		}
		// The reader's place is mirrored after the change, never before it: a caret at the end of a long insertion sits past
		// the end of the document until the document holds what was typed, and the document refuses a selection there.
		if (update.selectionSet || update.docChanged) this.mirrorToDocument(cell, editor);
	};

	/**
	 * One product event for each cell the reader typed in and leaves with text other than it opened with, its padding aside:
	 * typing and deleting back, or undoing, is no edit. No cell content or position goes with it.
	 */
	private committed() {
		if (this.edited && this.editor?.state.doc.toString().trim() !== this.opened) emitProductEvent('document', 'table_cell_edited');
		this.edited = false;
	}

	private detach() {
		const editor = this.editor;
		if (!editor) return;
		this.committed();
		const cell = this.cell ?? this.left, mounted = this.mounted;
		this.editor = null;
		this.cell = null;
		this.left = null;
		// The layer is reused for the next cell opened, so the closed editor's element leaves it rather than staying behind, empty.
		this.wrap?.remove();
		this.wrap = null;
		this.mounted = null;
		editor.destroy();
		this.layer.close();
		// The cell goes back to being rendered text — from the source as it stands now, not as it was when the cell opened.
		const table = mounted?.closest<HTMLElement>(TABLE);
		if (cell && table?.isConnected) repaintTableCell(table, this.host.state, cell);
	}

	destroy() {
		this.detach();
		this.host.dom.classList.remove('cm-md-cell-open');
		this.host.dom.removeEventListener('click', this.openPressed);
		document.removeEventListener('pointerdown', this.pressedElsewhere, true);
		this.swipe.destroy();
	}
}

/** Clicking a drawn table cell edits its text where it is read, with the document's own undo, sync and markdown rendering. */
const cellEditing = ViewPlugin.fromClass(CellEditors);

export const markdownCellEditing: Extension = [
	openCellState,
	cellEditing,
	EditorView.updateListener.of((update) => update.view.plugin(cellEditing)?.settle(update)),
];
