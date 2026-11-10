import { markdown } from '@codemirror/lang-markdown';
import { EditorState, Facet, Transaction, type Extension } from '@codemirror/state';
import { EditorView, ViewPlugin, keymap, type ViewUpdate } from '@codemirror/view';
import { GFM } from '@lezer/markdown';
import { yUndoManagerKeymap } from 'y-codemirror.next';
import { blockRange, cellElement, drawnTableAt, TABLE } from './widget-blocks';
import { CELL_IMAGE } from './widget-open';
import {
	CELL_EDITOR, CELL_SYNC, cellSource, cellToOpen, closeCell, openCellAt, openCellState, type OpenCell
} from './widget-cell-state';
import { cellDifference, cellShapeOnly } from './widget-cell-text';
import { adjacentCell, caretAfterTable, cellBelow, tableOfCell } from './widget-cell-nav';
import { repaintTableCell } from './widget-table';

/**
 * Editing the text of a rendered table cell where it is read.
 *
 * Clicking a drawn cell opens a small editor inside that cell's own `<td>`, holding exactly that cell's source slice
 * between its pipes. So one keystroke is one transaction against the cell's own range, and the document keeps its single
 * Yjs binding, its single undo manager and its single markdown parse: the nested editor holds no document state of its
 * own and is rewritten from the document whenever the two disagree, which is also how a live edit from another client
 * reaches the cell. Formatting renders while you type because the cell runs the document's own hide-syntax pass, handed
 * to it through `cellEditorExtensions`.
 *
 * The editor replaces the cell's rendered content while it is open, and the cell is repainted from the source when it
 * closes — which only works because `widget-table.ts` keeps the table's DOM, and this `<td>`, alive across edits.
 */

/** The extensions a cell's own editor runs over its source. `markdown-live.ts` supplies the document's hide-syntax pass. */
export const cellEditorExtensions = Facet.define<Extension, readonly Extension[]>({ combine: (values) => values });

/** The theme a cell editor needs to read as the cell it sits in; its type and colour come from the cell around it. */
const CELL_THEME = EditorView.theme({
	'&': { backgroundColor: 'transparent' },
	'.cm-scroller': { overflow: 'hidden', fontFamily: 'inherit', lineHeight: 'inherit' },
	'.cm-content': { padding: '0', caretColor: 'var(--accent)' },
	'.cm-line': { padding: '0' },
	'&.cm-focused': { outline: 'none' }
});

type Change = { from: number; to: number; insert: string };

/** The cell's editor and the `<td>` holding it, for as long as one cell is open. */
class CellEditors {
	private cell: OpenCell | null = null;
	private left: OpenCell | null = null;
	private editor: EditorView | null = null;
	private wrap: HTMLElement | null = null;
	private mounted: HTMLTableCellElement | null = null;

	constructor(private readonly host: EditorView) {
		host.dom.addEventListener('click', this.openPressed);
		document.addEventListener('pointerdown', this.pressedElsewhere, true);
	}

	/** A click on a drawn cell edits it: a caret in that cell, the way a click in prose places one. */
	private readonly openPressed = (event: MouseEvent) => {
		const host = this.host;
		const target = event.target instanceof Element ? event.target : null;
		// A click on something inside the cell that has its own job — a link, an image, a comment or suggestion drawn on the
		// cell's text — belongs to that flow, not to editing. The cell's own editor is excluded so a click in it stays a caret move.
		if (!target || target.closest(CELL_EDITOR) || target.closest(`a, img, ${CELL_IMAGE}, [data-comment-ids]`)) return;
		const widget = target.closest<HTMLElement>(TABLE);
		const cell = target.closest<HTMLTableCellElement>('td, th');
		const row = cell?.parentElement as HTMLTableRowElement | null;
		if (!widget || !host.dom.contains(widget) || !cell || !row || host.state.readOnly) return;
		const selection = document.getSelection();
		if (selection && !selection.isCollapsed && widget.contains(selection.anchorNode)) return; // a drag inside the table is a comment selection, not a click to edit
		const block = blockRange(host, widget);
		if (!block) return;
		const open = cellToOpen(host.state, block.from, row.rowIndex, cell.cellIndex);
		if (open) host.dispatch({ effects: openCellAt.of(open) });
	};

	private readonly pressedElsewhere = (press: Event) => {
		if (this.cell && !this.mounted?.contains(press.target as Node)) this.close();
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
		const was = this.cell;
		const moved = !was || was.from !== cell.from || was.to !== cell.to
			|| was.row !== cell.row || was.col !== cell.col || was.tableFrom !== cell.tableFrom;
		this.cell = cell;
		if (moved && was && this.editor && (was.row !== cell.row || was.col !== cell.col)) {
			// The cell the editor is leaving goes back to being rendered text before the editor moves on to the next one.
			const previous = drawnTableAt(this.host, was.tableFrom);
			if (previous) repaintTableCell(previous, update.state, was);
			this.left = was;
			this.mounted = null;
		}
		if (!this.editor) this.attach(cell);
		const editor = this.editor;
		if (!editor) return;
		this.mount(cell);
		if (moved || update.docChanged) {
			const text = cellSource(update.state, cell), current = editor.state.doc.toString();
			if (current !== text) editor.dispatch({ changes: [cellDifference(current, text)], userEvent: CELL_SYNC });
		}
		if (moved) this.mirrorToDocument(cell, editor);
	}

	/** Puts the editor in the cell it edits, or out of the document while that cell is not drawn. */
	private mount(cell: OpenCell) {
		const wrap = this.wrap;
		if (!wrap) return;
		// Looked up by the table's first character, not the cell's: an empty cell's range sits on a delimiter the renderer
		// does not draw, so its own position can resolve to the node after the table.
		const widget = drawnTableAt(this.host, cell.tableFrom);
		const td = widget ? cellElement(widget, cell) : null;
		if (!td) { wrap.remove(); this.mounted = null; return; }
		if (td === this.mounted && wrap.parentElement === td) return;
		const hadFocus = wrap.contains(document.activeElement);
		td.textContent = ''; // the rendered text steps aside; the editor shows the same text, live
		td.append(wrap);
		this.mounted = td;
		if (hadFocus) this.editor?.focus();
		this.editor?.requestMeasure();
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
	}

	private moveTo(next: OpenCell | null) {
		if (!next) { this.leave(); return; }
		this.left = this.cell;
		this.host.dispatch({ effects: openCellAt.of(next) });
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
		this.editor = new EditorView({
			parent: wrap,
			state: EditorState.create({
				doc: cellSource(host.state, cell),
				selection: { anchor: at, head: at },
				extensions: [
					markdown({ extensions: GFM }),
					...host.state.facet(cellEditorExtensions),
					EditorView.lineWrapping,
					EditorState.transactionFilter.of((tr) => (tr.docChanged && !tr.isUserEvent(CELL_SYNC) ? cellShapeOnly(tr) : tr)),
					EditorView.updateListener.of(this.fromCell),
					keymap.of([
						{ key: 'Escape', preventDefault: true, run: () => (this.leave(), true) },
						{ key: 'Tab', preventDefault: true, run: () => (this.moveTo(adjacentCell(host.state, this.cell!, 1)), true) },
						{ key: 'Shift-Tab', preventDefault: true, run: () => (this.moveTo(adjacentCell(host.state, this.cell!, -1)), true) },
						{ key: 'Enter', preventDefault: true, run: () => (this.moveTo(cellBelow(host.state, this.cell!)), true) },
						// Undo and redo are the document's own: the same commands the document's keymap runs, against the
						// document's view, so the one Yjs undo manager holds the cell's edits in the document's history.
						...yUndoManagerKeymap.map((binding) => ({ ...binding, run: () => binding.run?.(host) ?? false }))
					]),
					CELL_THEME
				]
			})
		});
		this.editor.focus();
	}

	/** What the cell's editor did: send its change to the document on the cell's own range, and its selection as a document range. */
	private readonly fromCell = (update: ViewUpdate) => {
		const cell = this.cell, editor = this.editor;
		if (!cell || !editor) return;
		if (update.selectionSet || update.docChanged) this.mirrorToDocument(cell, editor);
		if (!update.docChanged || update.transactions.every((tr) => tr.isUserEvent(CELL_SYNC))) return;
		const changes: Change[] = [];
		update.changes.iterChanges((fromA, toA, _fromB, _toB, insert) => {
			changes.push({ from: cell.from + fromA, to: cell.from + toA, insert: insert.sliceString(0, insert.length) });
		});
		if (cell.padded) this.openCellInSource(changes, cell);
		const event = update.transactions[update.transactions.length - 1]?.annotation(Transaction.userEvent);
		this.host.dispatch(event ? { changes, userEvent: event } : { changes });
	};

	/**
	 * A cell the renderer pads in has no delimiters of its own yet, so the text typed into it opens the cell in the
	 * source: the row gains its pipe and the text sits between the two, inside the one transaction the reader made.
	 */
	private openCellInSource(changes: Change[], cell: OpenCell) {
		const leading = this.host.state.sliceDoc(cell.from - 1, cell.from) === '|' ? ' ' : ' | ';
		for (const change of changes) {
			if (!change.insert || change.from !== cell.from) continue;
			change.insert = `${leading}${change.insert} |`;
			return;
		}
	}

	private detach() {
		const editor = this.editor;
		if (!editor) return;
		const cell = this.cell ?? this.left, mounted = this.mounted, wrap = this.wrap;
		this.editor = null;
		this.cell = null;
		this.left = null;
		this.wrap = null;
		this.mounted = null;
		editor.destroy();
		wrap?.remove();
		// The cell goes back to being rendered text — from the source as it stands now, not as it was when the cell opened.
		const table = mounted?.closest<HTMLElement>(TABLE);
		if (cell && table?.isConnected) repaintTableCell(table, this.host.state, cell);
	}

	destroy() {
		this.detach();
		this.host.dom.classList.remove('cm-md-cell-open');
		this.host.dom.removeEventListener('click', this.openPressed);
		document.removeEventListener('pointerdown', this.pressedElsewhere, true);
	}
}

/** Clicking a drawn table cell edits its text where it is read, with the document's own undo, sync and markdown rendering. */
const cellEditing = ViewPlugin.fromClass(CellEditors);

export const markdownCellEditing: Extension = [
	openCellState,
	cellEditing,
	EditorView.updateListener.of((update) => update.view.plugin(cellEditing)?.settle(update)),
];
