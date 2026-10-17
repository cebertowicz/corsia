import type { EditorView } from '@codemirror/view';
import type { EditorState, Text } from '@codemirror/state';
import type { SyntaxNode, SyntaxNodeRef } from '@lezer/common';
import { ensureSyntaxTree, syntaxTree } from '@codemirror/language';
import type { CellSpan } from './widget-cells';

export const TABLE = '.cm-md-table';
export const DIAGRAM = '.cm-md-mermaid';

/** The widget a node sits in, if any. */
export const widgetOf = (view: EditorView, node: Node | null): HTMLElement | null => {
	const element = node instanceof Element ? node : node?.parentElement ?? null;
	const widget = element?.closest<HTMLElement>(`${TABLE}, ${DIAGRAM}`) ?? null;
	return widget && view.contentDOM.contains(widget) ? widget : null;
};

/** The whole lines a table's or diagram's block widget replaces: the one definition of a widget's source range. */
export const blockSource = (doc: Text, node: { from: number; to: number }) =>
	({ from: doc.lineAt(node.from).from, to: doc.lineAt(Math.min(node.to, doc.length)).to });

/** Whether a FencedCode node is drawn as a diagram: the one definition of a diagram's identity, shared by the renderer and the scroll target. */
export function isMermaidFence(node: SyntaxNodeRef, doc: Text): boolean {
	const info = node.node.getChild('CodeInfo');
	return !!info && doc.sliceString(info.from, info.to).trim().toLowerCase() === 'mermaid';
}

/** The end of the run of non-blank lines holding `pos`: a table ends at a blank line, so parsing to here completes the one holding `pos`. */
function runEnd(doc: Text, pos: number): number {
	let line = doc.lineAt(Math.min(pos, doc.length));
	while (line.number < doc.lines && doc.line(line.number + 1).text.trim() !== '') line = doc.line(line.number + 1);
	return line.to;
}

/**
 * The source of the first table the range touches, from the editor's own syntax tree (what the widget is drawn from); prose and
 * code with pipes are not one. Where the tree has not reached the range yet, it parses to the end of that block and no further.
 */
export function tableAt(state: EditorState, from: number, to: number): { from: number; to: number } | null {
	let found: { from: number; to: number } | null = null;
	(ensureSyntaxTree(state, runEnd(state.doc, to), 100) ?? syntaxTree(state)).iterate({ from, to, enter: (node) => {
		if (found) return false;
		if (node.name !== 'Table') return;
		found = blockSource(state.doc, node);
		return false;
	} });
	return found;
}

/**
 * The source of the diagram whose block the range covers whole, from the editor's own syntax tree and the diagram identity the
 * renderer draws from (`isMermaidFence`). Text inside it, or a range that merely takes it in, is not the whole diagram.
 */
export function diagramAt(state: EditorState, from: number, to: number): { from: number; to: number } | null {
	let found: { from: number; to: number } | null = null;
	(ensureSyntaxTree(state, Math.min(to, state.doc.length), 100) ?? syntaxTree(state)).iterate({ from, to, enter: (node) => {
		if (found) return false;
		if (node.name !== 'FencedCode') return;
		if (isMermaidFence(node, state.doc)) found = blockSource(state.doc, node);
		return false;
	} });
	const block = found as { from: number; to: number } | null;
	return block && from === block.from && to === block.to ? block : null;
}

/** Whether the range lies inside the text of one code span or block, which is drawn as written: it has no inline marks to drop. */
export function codeTextAt(state: EditorState, from: number, to: number): boolean {
	const tree = ensureSyntaxTree(state, Math.min(to, state.doc.length), 100) ?? syntaxTree(state);
	for (let node: SyntaxNode | null = tree.resolveInner(from, 1); node; node = node.parent) {
		if (node.name === 'CodeText') return from >= node.from && to <= node.to;
		if (node.name === 'InlineCode') {
			const marks = node.getChildren('CodeMark');
			return marks.length === 2 && from >= marks[0].to && to <= marks[1].from;
		}
	}
	return false;
}

/** A block widget's source range: a table's own length, a fenced block's whole fence. */
export function blockRange(view: EditorView, el: HTMLElement): { from: number; to: number } | null {
	let from: number;
	try { from = view.posAtDOM(el); } catch { return null; }
	const { doc } = view.state;
	if (el.matches(TABLE)) return { from, to: Math.min(doc.length, from + Number(el.dataset.srcLength ?? 0)) };
	for (let node = syntaxTree(view.state).resolveInner(Math.min(from + 1, doc.length), 1); node; node = node.parent!) {
		if (node.name === 'FencedCode') return blockSource(doc, node);
		if (!node.parent) break;
	}
	return null;
}

export const cellElement = (table: HTMLElement, cell: CellSpan) => table.querySelector('table')?.rows[cell.row]?.cells[cell.col] ?? null;

/** The rendered table row a source position sits on, for the margin rail: its box and its place in the table. */
export function tableRowAt(view: EditorView, el: Element, pos: number): { rect: DOMRect; index: number } | null {
	if (!(el instanceof HTMLElement) || !el.matches(TABLE)) return null;
	const block = blockRange(view, el);
	if (!block || pos < block.from || pos > block.to) return null;
	const offset = pos - block.from;
	const lines = view.state.sliceDoc(block.from, block.to).split('\n');
	let line = 0, start = 0;
	while (line < lines.length - 1 && start + lines[line].length + 1 <= offset) start += lines[line++].length + 1;
	const table = el.querySelector('table');
	const index = Math.max(0, Math.min(line === 0 ? 0 : line - 1, (table?.rows.length ?? 1) - 1));
	const tr = table?.rows[index];
	return tr ? { rect: tr.getBoundingClientRect(), index } : null;
}

/** The box a position inside a rendered table (its row) or diagram (the whole diagram) is drawn at, when that block is on screen. */
export function widgetBoxAt(view: EditorView, pos: number): DOMRect | null {
	for (const el of view.contentDOM.querySelectorAll<HTMLElement>(`${TABLE}, ${DIAGRAM}`)) {
		const block = blockRange(view, el);
		if (!block || pos < block.from || pos > block.to) continue;
		return el.matches(TABLE) ? tableRowAt(view, el, pos)?.rect ?? null : el.getBoundingClientRect();
	}
	return null;
}

/** The whole lines of the source a table or diagram widget replaces around a position, or null when the position is in neither. */
export function widgetSourceAt(view: EditorView, pos: number): { from: number; to: number } | null {
	const { doc } = view.state;
	for (let node = syntaxTree(view.state).resolveInner(pos, 1); node; node = node.parent!) {
		if (node.name === 'Table' || (node.name === 'FencedCode' && isMermaidFence(node, doc))) return blockSource(doc, node);
		if (node.name === 'FencedCode' || !node.parent) break;
	}
	return null;
}
