import { Decoration, type DecorationSet, type EditorView } from '@codemirror/view';
import type { EditorState, Range } from '@codemirror/state';
import { syntaxTree } from '@codemirror/language';
import { renderMarkdown } from '$lib/markdown';
import { imageContext, refreshMarkdownImages, type ImageContext } from './markdown-image';
import { renderedFragment } from './rendered-images';
import { blockSource, tableAt } from './widget-blocks';
import { BlockWidget, priorWidget, syntaxDecorationField } from './block-widget';
import { tableGrid, type CellSpan } from './widget-cells';
import { CELL_EDITING } from './widget-cell-layer';
import { type OpenCell } from './widget-cell-state';

/**
 * The rendered table, and the one way its DOM is built and kept current.
 *
 * A table is one block widget over its source lines, so a keystroke in a cell would otherwise recreate the whole table —
 * which is why an editor cannot live inside a cell while the widget is thrown away on every change. So the widget answers
 * CodeMirror's `updateDOM` and moves in only the cells whose text actually changed, leaving every other node where it is —
 * including the cell being edited, whose hidden text has to keep pace with the editor lying over it so the row grows as that
 * text wraps. CodeMirror calls `updateDOM` only when `eq` says the two widgets differ, and hands it the widget that drew the
 * node, so `eq` stays the strict question "does this render the same" and `updateDOM` patches the difference. A change of
 * *shape* — a row added, a column gained — and a change of the image context, whose URLs are baked into the rendered cells,
 * both refuse the patch and rebuild instead.
 */

class TableWidget extends BlockWidget {
	readonly images: ImageContext | null;
	constructor(readonly src: string, images: ImageContext | null, prior: TableWidget | null = null) {
		super(prior);
		// The context provider is live; retain this render's values so a refresh can compare them with the next one.
		this.images = images && { ...images };
	}
	eq(o: TableWidget) {
		return o.src === this.src && sameImages(this.images, o.images);
	}
	toDOM() {
		const el = renderTable(this.src, this.images);
		el.dataset.srcLength = String(this.src.length); // the source range this DOM stands for, for `blockRange`
		return this.drew(el);
	}
	updateDOM(dom: HTMLElement, _view: EditorView, from: TableWidget) {
		if (!sameImages(this.images, from.images) || shape(this.src) !== shape(from.src)) return false;
		if (!transplant(dom, this.src, this.images, from.src)) return false;
		dom.dataset.srcLength = String(this.src.length);
		this.drew(dom); // this node is what the next render measures its height against
		return true;
	}
	ignoreEvent() { return true; }
}

const sameImages = (a: ImageContext | null, b: ImageContext | null) =>
	a?.docPath === b?.docPath && a?.projectId === b?.projectId && a?.token === b?.token && a?.imageCacheKey === b?.imageCacheKey;

/** The one rendering path a table has: the app's canonical markdown renderer, over the table's own source lines. */
function renderTable(src: string, images: ImageContext | null): HTMLElement {
	const el = document.createElement('div');
	el.className = 'cm-md-table md-content';
	// Same trust boundary as every `{@html} renderMarkdown(...)` surface: renderMarkdown applies the canonical DOMPurify
	// policy before HTML reaches the document. Keep this widget on that single sanitized renderer.
	el.append(renderedFragment(renderMarkdown(src), images));
	return el;
}

/**
 * The row count and the number of drawn cells in each: what a cell-by-cell patch can follow, and what a real edit breaks.
 * It reads the same drawn cells (`tableGrid`) `transplant` walks, so a row that gains or loses source cells keeps its shape
 * and has every drawn cell compared, padded ones included.
 */
const shape = (src: string) => {
	const perRow: number[] = [];
	for (const span of tableGrid(src)) perRow[span.row] = (perRow[span.row] ?? 0) + 1;
	return `${src.split('\n').length}|${perRow.join(',')}`;
}

/** A drawn cell's source between its pipes; a padded cell has none, so it reads as empty. */
const drawnSource = (src: string, span: CellSpan) => src.slice(span.rawFrom, span.rawTo);

/**
 * Re-renders `src` and moves into the drawn table only the cells whose source text differs from `was`, the source the node
 * holds — the cell being edited included, since its hidden text is what keeps the row the right height.
 */
function transplant(el: HTMLElement, src: string, images: ImageContext | null, was: string, force?: { row: number; col: number }): boolean {
	const fresh = renderTable(src, images).querySelector('table');
	const drawn = el.querySelector('table');
	if (!fresh || !drawn) return false;
	const held = tableGrid(was);
	// The delimiter row holds each column's alignment and no cell's text, so a change to it alone moves no cell's source:
	// it is compared as its own line, and every drawn cell takes the new alignment attributes, the empty cells the renderer
	// pads a short row with included, which have no source cell of their own.
	const realigned = delimiterRow(src) !== delimiterRow(was);
	let copied = false;
	for (const span of tableGrid(src)) {
		const before = held.find((item) => item.row === span.row && item.col === span.col);
		const unchanged = before && !before.padded === !span.padded && drawnSource(was, before) === drawnSource(src, span);
		const forced = force && span.row === force.row && span.col === force.col;
		const next = fresh.rows[span.row]?.cells[span.col];
		const now = drawn.rows[span.row]?.cells[span.col];
		if (!next || !now) continue;
		if (unchanged && !forced) continue;
		copyCell(now, next);
		copied = true;
	}
	if (realigned) {
		for (const [row, tr] of Array.from(fresh.rows).entries()) {
			for (const [col, next] of Array.from(tr.cells).entries()) {
				const now = drawn.rows[row]?.cells[col];
				if (now) { copyAttributes(now, next); copied = true; }
			}
		}
	}
	// A copied cell arrives unpainted, so the comment paint pass must not take the table for already drawn: the same
	// marks over the same source (a closed cell, an edit that keeps the length) would otherwise leave its comments off.
	if (copied) delete el.dataset.paintSignature;
	return true;
}

/**
 * Moves a re-rendered cell into the drawn one and keeps the drawn element. A cell's own node has to survive the keystroke
 * that changed it: the reader's editor lies over it and follows its box, and a comment anchor inside it is a live node.
 * Only its attributes and its inside are replaced, which is also the smaller mutation for the browser to lay out.
 */
function copyCell(into: HTMLTableCellElement, from: HTMLTableCellElement) {
	copyAttributes(into, from);
	into.replaceChildren(...from.childNodes);
}

/** The table's delimiter line (`| :--- | ---: |`), the second line of its source. */
const delimiterRow = (src: string) => src.split('\n')[1] ?? '';

/** A cell's attributes only (its alignment among them), leaving its content and anything live inside it where it is. */
function copyAttributes(into: HTMLTableCellElement, from: HTMLTableCellElement) {
	// The marker that an editor lies over this cell belongs to the reader's place, not to the source, so it rides through
	// the patch untouched; the layer takes it off when the cell closes or the editor moves on.
	if (into.classList.contains(CELL_EDITING)) from.classList.add(CELL_EDITING);
	for (const attribute of Array.from(from.attributes)) if (into.getAttribute(attribute.name) !== attribute.value) into.setAttribute(attribute.name, attribute.value);
	for (const { name } of Array.from(into.attributes)) if (!from.hasAttribute(name)) into.removeAttribute(name);
}

/**
 * Puts a cell's rendered content back from the document's source: what a closed cell needs, since its editor lay over its
 * text while it was open and the source may have moved on underneath it.
 */
export function repaintTableCell(el: HTMLElement, state: EditorState, cell: OpenCell) {
	const block = tableAt(state, cell.from, cell.from + 1);
	if (!block || block.from !== cell.tableFrom || !el.querySelector('table')) return;
	const src = state.sliceDoc(block.from, block.to);
	transplant(el, src, state.facet(imageContext)(), src, cell);
}

/**
 * Replaces each GFM table with a rendered block widget. Block decorations must come from a state field (CodeMirror forbids
 * them from view plugins), and the replaced range spans whole lines so the widget cleanly takes the block's place. It
 * rebuilds when the document changes or the background parser advances: a table below the initial parse budget enters the
 * tree later, on a transaction that carries no document change.
 */
function buildTableDecorations(state: EditorState, prior?: DecorationSet): DecorationSet {
	const decos: Range<Decoration>[] = [];
	const { doc } = state;
	const images = state.facet(imageContext)();
	syntaxTree(state).iterate({
		enter: (node) => {
			if (node.name !== 'Table') return;
			const { from, to } = blockSource(doc, node);
			const src = doc.sliceString(from, to);
			decos.push(Decoration.replace({ widget: new TableWidget(src, images, priorWidget(prior, from, to, TableWidget)), block: true }).range(from, to));
			return false;
		}
	});
	return Decoration.set(decos, true);
}

export const markdownTableDecorations = syntaxDecorationField(buildTableDecorations, (tr) => tr.effects.some((effect) => effect.is(refreshMarkdownImages)));
