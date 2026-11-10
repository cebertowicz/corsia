import { markdown } from '@codemirror/lang-markdown';
import { EditorState, Facet, StateEffect, StateField, Transaction, type Extension } from '@codemirror/state';
import { EditorView, ViewPlugin, keymap, type ViewUpdate } from '@codemirror/view';
import { GFM } from '@lezer/markdown';
import { yUndoManagerKeymap } from 'y-codemirror.next';
import { blockRange, cellElement, drawnTableAt, tableAt, TABLE } from './widget-blocks';
import { CELL_IMAGE } from './widget-open';
import { intersectsCell, tableCells, type CellSpan } from './widget-cells';

/**
 * Editing the text of a rendered table cell without dropping the table to raw pipe source.
 *
 * The table stays the one block widget it was: clicking a drawn cell opens a small editor held over that cell whose
 * document is exactly that cell's source slice in the main document (pipes and padding included). So one keystroke is
 * one transaction against the cell's own range, and the document keeps its single Yjs binding, its single undo manager
 * and its single parse — the nested editor holds no document state of its own and is rewritten from the main document
 * whenever the two disagree, which is also how a live edit from another client reaches the cell.
 *
 * Where the cell is in the source is tracked twice over: the open cell is a state field whose range is mapped through
 * every main-document transaction, and on each change that mapped range is re-derived from the editor's own syntax tree
 * (`tableAt` for the table's lines, `tableCells` for the cell's place in them). Mapping alone follows the text; the
 * re-derivation keeps the span on the cell's own pipes when a collaborator's update moves a row or a column, and answers
 * "no cell here" — which closes the editor — when the cell or its table is gone.
 */

/** The cell under edit: its table's source start, its place in the drawn table, and the source range its editor holds. */
interface OpenCell { tableFrom: number; row: number; col: number; from: number; to: number }

const openCellAt = StateEffect.define<OpenCell>();
const closeCell = StateEffect.define<null>();

/** The extensions a cell's own editor runs over its source. `markdown-live.ts` supplies the document's hide-syntax pass. */
export const cellEditorExtensions = Facet.define<Extension, readonly Extension[]>({ combine: (values) => values });

/** The write of the document's cell text back into the cell editor: never sent back to the document, never rewritten. */
const SYNC = 'table-cell-sync';

/** The cell's whole source between its two pipes, padding included: what the cell editor holds, byte for byte. */
const cellSource = (state: EditorState, cell: OpenCell) => state.sliceDoc(cell.from, cell.to);

/**
 * The cell the mapped range best covers, from the editor's own parse. Raw spans (pipes' inner edges) rather than the
 * trimmed content ones are what the editor can hold: a space typed at the edge of a cell has to land somewhere.
 */
function resolveCell(state: EditorState, cell: OpenCell): OpenCell | null {
	const block = tableAt(state, cell.from, Math.max(cell.from, cell.to));
	if (!block) return null;
	const spans = tableCells(state.sliceDoc(block.from, block.to));
	const from = cell.from - block.from, to = Math.max(from, cell.to - block.from);
	let best: { span: CellSpan; over: number } | null = null;
	for (const span of spans) {
		const over = Math.min(span.rawTo, to) - Math.max(span.rawFrom, from);
		if (over > 0 && (!best || over > best.over)) best = { span, over };
	}
	// An empty cell has no width to overlap: its source anchor is what says the range is still that cell's.
	const span = best?.span ?? spans.find((item) => intersectsCell(item, from, to));
	if (!span) return null;
	return { tableFrom: block.from, row: span.row, col: span.col, from: block.from + span.rawFrom, to: block.from + span.rawTo };
}

const openCell = StateField.define<OpenCell | null>({
	create: () => null,
	update(value, transaction) {
		for (const effect of transaction.effects) {
			if (effect.is(openCellAt)) value = effect.value;
			else if (effect.is(closeCell)) value = null;
		}
		if (!value || !transaction.docChanged) return value;
		const { changes } = transaction;
		return resolveCell(transaction.state, {
			...value,
			tableFrom: changes.mapPos(value.tableFrom, 1),
			from: changes.mapPos(value.from, 1),
			to: changes.mapPos(value.to, -1)
		});
	}
});

/** The smallest change from one cell text to the next, so the caret the reader placed maps through it instead of jumping to an end. */
function cellDifference(oldText: string, newText: string) {
	let keep = 0;
	while (keep < oldText.length && keep < newText.length && oldText[keep] === newText[keep]) keep++;
	let tail = 0;
	while (tail < oldText.length - keep && tail < newText.length - keep && oldText[oldText.length - 1 - tail] === newText[newText.length - 1 - tail]) tail++;
	return { from: keep, to: oldText.length - tail, insert: newText.slice(keep, newText.length - tail) };
}

const ONLY_BREAKS = /^\n+$/;

/**
 * What the cell editor accepts: a pipe is stored escaped, because an unescaped one would end the cell; a line break has
 * nowhere to go, so Enter does nothing and a pasted block becomes one line; and a deletion that would leave half of an
 * escaped pipe behind takes the pair, since `|` on its own splits the row.
 */
function cellShapeOnly(transaction: Transaction) {
	const doc = transaction.startState.doc;
	const changes: { from: number; to: number; insert: string }[] = [];
	let rewritten = false;
	transaction.changes.iterChanges((fromA, toA, _fromB, _toB, insert) => {
		let from = fromA, to = toA;
		const before = insert.sliceString(0, insert.length);
		let text = before;
		if (from !== to) {
			if (doc.sliceString(to - 1, to) === '\\' && doc.sliceString(to, to + 1) === '|') to++;
			if (doc.sliceString(from - 1, from) === '\\' && doc.sliceString(from, from + 1) === '|') from--;
		}
		if (text.includes('\n')) text = ONLY_BREAKS.test(text) ? '' : text.replace(/\n+/g, ' ');
		if (text.includes('|')) text = text.replace(/\|/g, '\\|');
		rewritten ||= from !== fromA || to !== toA || text !== before;
		changes.push({ from, to, insert: text });
	});
	if (!rewritten) return transaction;
	// The caret follows the rewritten change rather than a position computed for the text the reader typed.
	const userEvent = transaction.annotation(Transaction.userEvent);
	return [userEvent ? { changes, userEvent } : { changes }];
}

/** The theme a cell editor needs to read as the cell it sits in; its type and colour are copied from the cell below it. */
const CELL_THEME = EditorView.theme({
	'&': { backgroundColor: 'var(--bg-surface)', boxShadow: '0 0 0 2px var(--accent-wash)', borderRadius: '3px' },
	'.cm-scroller': { overflow: 'hidden', fontFamily: 'inherit', lineHeight: 'inherit' },
	'.cm-content': { padding: '1px 2px', caretColor: 'var(--accent)' },
	'.cm-line': { padding: '0' },
	'&.cm-focused': { outline: 'none' }
});

/** The document text and computed type copied onto the overlay, so the editor over a cell reads as that cell. */
const COPIED = ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'color'] as const;

class CellEditorOver {
	private cell: OpenCell | null = null;
	private editor: EditorView | null = null;
	private overlay: HTMLElement | null = null;

	constructor(readonly host: EditorView) {
		host.dom.addEventListener('click', this.openPressed);
		document.addEventListener('pointerdown', this.pressedElsewhere, true);
		document.addEventListener('scroll', this.place, true);
	}

	/** The cell the reader clicked, read back to the source the same way a cell image or a cell comment is. */
	private readonly openPressed = (press: MouseEvent) => {
		const host = this.host, state = host.state;
		const target = press.target as Element | null;
		const cell = target?.closest?.<HTMLElement>('td, th');
		const widget = cell?.closest<HTMLElement>(TABLE);
		// A link, an image and a drag selection inside the table already have their own answer to a click.
		if (!cell || !widget || !host.contentDOM.contains(widget) || host.state.readOnly) return;
		if (target?.closest(`a, img, ${CELL_IMAGE}`)) return;
		const selection = document.getSelection();
		if (selection && !selection.isCollapsed && widget.contains(selection.anchorNode)) return;
		const block = blockRange(host, widget);
		const row = cell.parentElement as HTMLTableRowElement | null;
		if (!block || !row) return;
		const col = (cell as HTMLTableCellElement).cellIndex;
		const span = tableCells(state.sliceDoc(block.from, block.to)).find((item) => item.row === row.rowIndex && item.col === col);
		if (!span) return;
		const open = resolveCell(state, { tableFrom: block.from, row: span.row, col: span.col, from: block.from + span.rawFrom, to: block.from + span.rawTo });
		if (open) host.dispatch({ effects: openCellAt.of(open) });
	};

	private readonly pressedElsewhere = (press: Event) => {
		if (this.overlay && !this.overlay.contains(press.target as Node)) this.close();
	};

	update(update: ViewUpdate) {
		const cell = update.state.field(openCell);
		if (!cell) { this.detach(); return; }
		const moved = cell !== this.cell;
		this.cell = cell;
		if (!this.editor) this.attach(cell);
		const editor = this.editor;
		if (!editor) return;
		const text = cellSource(update.state, cell);
		if (moved || update.docChanged) {
			const current = editor.state.doc.toString();
			if (current !== text) editor.dispatch({ changes: [cellDifference(current, text)], userEvent: SYNC });
			this.place();
		}
	}

	destroy() {
		this.detach();
		this.host.dom.removeEventListener('click', this.openPressed);
		document.removeEventListener('pointerdown', this.pressedElsewhere, true);
		document.removeEventListener('scroll', this.place, true);
	}

	private close() {
		this.cell = null;
		if (this.host.state.field(openCell)) this.host.dispatch({ effects: closeCell.of(null) });
	}

	/** The cell's own editor: the document's markdown parse and hide-syntax pass, and no history of its own. */
	private attach(cell: OpenCell) {
		const host = this.host;
		const overlay = document.createElement('div');
		overlay.className = 'cm-md-cell-editor md-content';
		document.body.append(overlay);
		this.overlay = overlay;
		this.editor = new EditorView({
			parent: overlay,
			state: EditorState.create({
				doc: cellSource(host.state, cell),
				extensions: [
					markdown({ extensions: GFM }),
					...host.state.facet(cellEditorExtensions),
					EditorView.lineWrapping,
					EditorState.transactionFilter.of((tr) => (tr.docChanged && !tr.isUserEvent(SYNC) ? cellShapeOnly(tr) : tr)),
					EditorView.updateListener.of(this.forwarded),
					keymap.of([
						{ key: 'Escape', preventDefault: true, run: () => (this.close(), true) },
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

	/** Every change made in the cell, sent to the document as a change on the cell's own range. */
	private readonly forwarded = (update: ViewUpdate) => {
		const cell = this.cell, editor = this.editor;
		if (!cell || !editor || !update.docChanged || update.transactions.every((tr) => tr.isUserEvent(SYNC))) return;
		const changes: { from: number; to: number; insert: string }[] = [];
		update.changes.iterChanges((fromA, toA, _fromB, _toB, insert) => {
			changes.push({ from: cell.from + fromA, to: cell.from + toA, insert: insert.sliceString(0, insert.length) });
		});
		const event = update.transactions[update.transactions.length - 1]?.annotation(Transaction.userEvent);
		this.host.dispatch(event ? { changes, userEvent: event } : { changes });
	};

	private readonly place = () => {
		this.host.requestMeasure({
			key: this,
			read: (view) => {
				const cell = view.state.field(openCell);
				const widget = cell ? drawnTableAt(view, cell.from) : null;
				const td = widget && cell ? cellElement(widget, cell) : null;
				return td ? { box: td.getBoundingClientRect(), td } : null;
			},
			write: (found) => {
				const overlay = this.overlay, editor = this.editor;
				if (!overlay || !editor) return;
				const { style } = overlay;
				style.position = 'fixed';
				// Out of sight rather than closed: a table scrolled off screen keeps its open cell.
				style.visibility = found ? 'visible' : 'hidden';
				if (!found) return;
				style.top = `${found.box.top}px`;
				style.left = `${found.box.left}px`;
				style.width = `${found.box.width}px`;
				style.minHeight = `${found.box.height}px`;
				const computed = getComputedStyle(found.td);
				for (const name of COPIED) style[name] = computed[name];
			}
		});
	};

	private detach() {
		if (!this.editor) return;
		this.cell = null;
		this.editor.destroy();
		this.editor = null;
		this.overlay?.remove();
		this.overlay = null;
	}
}

/** Clicking a drawn table cell edits its text where it is read, with the document's own undo, sync and markdown rendering. */
export const markdownCellEditing: Extension = [openCell, ViewPlugin.fromClass(CellEditorOver)];
