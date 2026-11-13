import { EditorState, Facet, Transaction, type Extension } from '@codemirror/state';
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { emitProductEvent } from '$lib/product_events';
import { cellElement, drawnTableAt, TABLE } from './widget-blocks';
import {
	CELL_SYNC, cellSource, closeCell, mapRowStart, openCellAt, openCellState, type OpenCell
} from './widget-cell-state';
import { heldText, heldToSource, runDifference, sourceToHeld, writeCellEdit } from './widget-cell-row';
import { adjacentCell, caretAfterTable, cellBelow, cellLabel, tableOfCell, typingBelowFinalTable } from './widget-cell-nav';
import { cellKeys } from './widget-cell-keys';
import { CellPresses } from './widget-cell-presses';
import { CELL_THEME } from './widget-cell-theme';
import { repaintTableCell } from './widget-table';
import { cellHighlight } from './theme';
import { INLINE_MARKDOWN } from './widget-cell-markdown';
import { cellMarksField } from './widget-cell-comment';

/**
 * Editing the text of a rendered table cell where it is read.
 *
 * Clicking a drawn cell opens a small editor inside that cell's own `<td>`, holding that cell's content as GFM reads it: its
 * source between its pipes with the table's own `\|` escapes out and its pads trimmed (`widget-cell-row.ts`). Each keystroke
 * writes that content back into the row through the one serializer, `writeCellEdit`, as one transaction of the minimal
 * changes to the row, so the document keeps its single Yjs binding, its single undo manager and its single markdown parse:
 * the nested editor holds no document state of its own and is rewritten from the document whenever the two disagree, which
 * is also how a live edit from another client reaches the cell. Formatting renders while you type because the cell parses
 * inline markdown (`widget-cell-markdown.ts`) and runs the document's own hide-syntax pass (`cellEditorExtensions`).
 *
 * The editor replaces the `<td>`'s drawn content while it is open, so it wraps, grows the row and takes the cell's look
 * like the text it replaces. `widget-table.ts` keeps the table's DOM, and this `<td>`, alive across edits and leaves the
 * open cell's content alone; the cell is repainted from the source when the editor closes.
 */

/** A cell editor's added extensions: the hide-syntax pass (`markdown-live.ts`) and the save key (`MarkdownEditor.svelte`), as cell keys skip the doc's keymap. */
export const cellEditorExtensions = Facet.define<Extension, readonly Extension[]>({ combine: (values) => values });

type Change = { from: number; to: number; insert: string };

/** The cell's editor, and the cell it is held over, for as long as one cell is open. */
class CellEditors {
	private cell: OpenCell | null = null;
	private left: OpenCell | null = null;
	private fresh = false;
	// Where the reader's own edit leaves their selection in the content the document now holds, for the write back into the editor.
	private written: { text: string; anchor: number; head: number } | null = null;
	// Where the editor's text starts in the cell's source.
	private base = 0;
	// Whether the reader has typed in the open cell, and the text it held when it opened: leaving it with other text is an edit.
	private edited = false;
	private opened = '';
	private editor: EditorView | null = null;
	private wrap: HTMLElement | null = null;
	private mounted: HTMLTableCellElement | null = null;
	// Where the click that opened a cell landed, so the caret goes there and not to the start of the cell's text.
	private clickedAt: { x: number; y: number } | null = null;
	private readonly presses: CellPresses;

	constructor(private readonly host: EditorView) {
		this.presses = new CellPresses(host, {
			isOpen: () => !!this.cell,
			inEditor: (node) => !!this.wrap?.contains(node),
			opened: (at) => { this.clickedAt = at; },
			close: () => this.close()
		});
	}

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
		// The same cell is the same row's line, wherever the update moved it, and the same column.
		const another = !was || was.col !== cell.col || mapRowStart(update.changes, update.state.doc, was.rowStart) !== cell.rowStart;
		const moved = another || was.from !== cell.from || was.to !== cell.to || was.tableFrom !== cell.tableFrom;
		this.cell = cell;
		const leaving = another && was && this.editor ? was : null;
		if (leaving) {
			this.committed();
			this.left = leaving;
			this.mounted = null;
		}
		if (!this.editor) this.attach(cell);
		const editor = this.editor;
		if (!editor) return;
		const mounted = this.mount(cell);
		// The cell the editor left goes back to being rendered text once the editor is out of it.
		const previous = leaving && drawnTableAt(this.host, leaving.tableFrom);
		if (leaving && previous) repaintTableCell(previous, update.state, leaving);
		if (!mounted) return;
		if (moved || update.docChanged) this.syncEditor(update.state, cell, editor, !another);
		if (another) this.opened = editor.state.doc.toString().trim();
		if (moved) this.mirrorToDocument(cell, editor);
	}

	/** Puts the editor in the cell it edits, in place of the cell's drawn content. A cell the table does not draw closes, as nothing could show the caret. False when it closed. */
	private mount(cell: OpenCell): boolean {
		const wrap = this.wrap;
		if (!wrap) return true;
		// Looked up by the table's first character, not the cell's: an empty cell's range sits on a delimiter the renderer
		// does not draw, so its own position can resolve to the node after the table.
		const widget = drawnTableAt(this.host, cell.tableFrom);
		const td = widget ? cellElement(widget, cell) : null;
		if (!td) { this.close(); return false; }
		if (td === this.mounted && wrap.parentElement === td) { this.clickedAt = null; return true; }
		const hadFocus = wrap.contains(document.activeElement);
		td.replaceChildren(wrap);
		this.mounted = td;
		// Named for the cell it is in, so a screen reader says which cell is being edited, and again after Tab or Enter.
		this.editor?.contentDOM.setAttribute('aria-label', cellLabel(td));
		// Focus is taken only once the editor is in the document — before that, focusing it does nothing — and the click
		// that opened the cell counts as the reader asking for the caret there, at the word they clicked rather than at the
		// start of the cell. The editor is in the cell by now, so it reads the click's point off its own layout.
		if (hadFocus || this.fresh) {
			this.editor?.focus();
			this.fresh = false;
			this.placeAtClick();
		}
		this.editor?.requestMeasure();
		return true;
	}

	/**
	 * Puts the document's content of the cell into its editor where they differ. After the reader's own edit the selection goes
	 * where that edit left it in the content as stored (`written`); for anything else it maps through the change. A space the
	 * reader typed at the cell's edge is kept only while the editor stays on that cell (`same`): the next cell holds its own text.
	 */
	private syncEditor(state: EditorState, cell: OpenCell, editor: EditorView, same: boolean) {
		const current = editor.state.doc.toString(), written = same ? this.written : null;
		this.written = null;
		const { text, base } = heldText(cellSource(state, cell), same ? (written?.text ?? current) : undefined);
		this.base = base;
		if (current === text && !written) return;
		const selection = written && written.text === text ? { anchor: written.anchor, head: written.head } : undefined;
		editor.dispatch({ changes: current === text ? [] : runDifference(current, text), selection, userEvent: CELL_SYNC });
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
		const held = { text: editor.state.doc.toString(), base: this.base }, main = editor.state.selection.main;
		const anchor = cell.from + heldToSource(held, main.anchor), head = cell.from + heldToSource(held, main.head);
		const selection = this.host.state.selection.main;
		if (selection.anchor === anchor && selection.head === head) return;
		this.host.dispatch({ selection: { anchor, head }, userEvent: CELL_SYNC });
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
		// The caret goes to the end of the next cell's text.
		this.editor?.dispatch({ selection: { anchor: this.editor.state.doc.length } });
		this.editor?.focus();
	}

	/** The cell's own editor: an inline markdown parse, the document's hide-syntax pass, and no history of its own. */
	private attach(cell: OpenCell) {
		const host = this.host;
		const source = cellSource(host.state, cell), held = heldText(source);
		this.base = held.base;
		// The caret goes where the click landed: the document's selection is the click's position, mapped into the cell.
		const clicked = host.state.selection.main;
		const at = clicked.from >= cell.from && clicked.from <= cell.to ? sourceToHeld(source, held, clicked.from - cell.from) : 0;
		// The element a cell's editor is recognised by: the comment flows and the paint pass leave it alone.
		const wrap = document.createElement('div');
		wrap.className = 'cm-md-cell-editor';
		this.wrap = wrap;
		this.fresh = true;
		this.editor = new EditorView({
			parent: wrap,
			state: EditorState.create({
				doc: held.text,
				selection: { anchor: at, head: at },
				extensions: [
					INLINE_MARKDOWN,
					...host.state.facet(cellEditorExtensions),
					cellHighlight,
					cellMarksField,
					EditorView.lineWrapping,
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
			// The whole content is written back into the row through the one row mapping; the range the edit touched keeps the
			// change sent to the document the edit itself.
			let from = Infinity, to = -Infinity;
			update.changes.iterChangedRanges((fromA, toA) => { from = Math.min(from, fromA); to = Math.max(to, toA); });
			const before = { text: update.startState.doc.toString(), base: this.base };
			const line = this.host.state.doc.lineAt(cell.rowStart);
			const edited = { from: heldToSource(before, from), to: heldToSource(before, to) };
			const write = writeCellEdit(line.text, cell.col, update.state.doc.toString(), edited);
			const { anchor, head } = update.state.selection.main;
			this.written = { text: write.text, anchor: write.at(anchor), head: write.at(head) };
			this.edited = true;
			const changes: Change[] = write.changes.map((change) => ({ ...change, from: line.from + change.from, to: line.from + change.to }));
			const event = update.transactions[update.transactions.length - 1]?.annotation(Transaction.userEvent);
			// An edit the row already holds (a backslash typed in front of a pipe, which was already its escape) changes no
			// document, so the editor is put back to the content as stored here.
			if (changes.length) this.host.dispatch(event ? { changes, userEvent: event } : { changes });
			else this.syncEditor(this.host.state, cell, editor, true);
		}
		// The reader's place is mirrored after the change, onto the cell as it then stands (a row that gained its leading pipe
		// moved it along), never before: the document refuses a caret past what it holds yet, as after a long insertion.
		const now = this.cell;
		if (now && (update.selectionSet || update.docChanged)) this.mirrorToDocument(now, editor);
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
		this.wrap?.remove();
		this.wrap = null;
		this.mounted = null;
		editor.destroy();
		// The cell goes back to being rendered text — from the source as it stands now, not as it was when the cell opened.
		const table = mounted?.closest<HTMLElement>(TABLE);
		if (cell && table?.isConnected) repaintTableCell(table, this.host.state, cell);
	}

	destroy() {
		this.detach();
		this.host.dom.classList.remove('cm-md-cell-open');
		this.presses.destroy();
	}
}

/** Clicking a drawn table cell edits its text where it is read, with the document's own undo, sync and markdown rendering. */
const cellEditing = ViewPlugin.fromClass(CellEditors);

export const markdownCellEditing: Extension = [
	openCellState, typingBelowFinalTable,
	cellEditing,
	EditorView.updateListener.of((update) => update.view.plugin(cellEditing)?.settle(update)),
];
