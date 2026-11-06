import { EditorView, ViewPlugin, type BlockInfo, type ViewUpdate } from '@codemirror/view';
import { commentRanges } from './comment-marks';
import { railWidth } from './comment-clusters';
import { tableRowAt } from './widget-blocks';

interface Placed {
	/** Top of the line's first visual row, in scroller coordinates. */
	top: number;
	ids: string[];
	/** Every thread on this line has lost its text. */
	orphan: boolean;
	/** Height of one normal row. */
	row: number;
}

/**
 * Where each commented line is, for the margin cards: one empty, hidden, non-interactive element per commented line, on that line's
 * first row, carrying the ids of its threads. A thread is opened by its highlight, its card or the comments list; these draw nothing
 * and take no focus or pointer. They are regrouped whenever the viewport or geometry changes.
 */
class CommentRail {
	private readonly dom: HTMLElement;
	private lines: HTMLElement[] = [];
	private readonly resize?: ResizeObserver;
	private watched = new Set<Element>();

	constructor(private readonly view: EditorView) {
		this.dom = document.createElement('div');
		this.dom.className = 'cm-comment-rail';
		this.dom.setAttribute('aria-hidden', 'true');
		view.scrollDOM.append(this.dom);
		// The content padding still reserves the text's left inset through --doc-rail; set it here so an editor mounted outside the
		// viewer reserves the same width the viewer's header takes.
		view.dom.style.setProperty('--doc-rail', `${railWidth()}px`);
		// A text size or line height change moves lines without a document or viewport change: regroup on any resize of the
		// text, or of a commented line or image (the text's own height can be pinned by its minimum height while its lines change).
		if (typeof ResizeObserver !== 'undefined') {
			this.resize = new ResizeObserver(() => this.schedule());
			this.resize.observe(view.contentDOM);
		}
		this.schedule();
	}

	update(update: ViewUpdate) {
		const ranges = update.startState.field(commentRanges) !== update.state.field(commentRanges);
		if (ranges || update.docChanged || update.viewportChanged || update.heightChanged || update.geometryChanged) this.schedule();
	}

	destroy() {
		this.resize?.disconnect();
		this.watched.clear();
		this.dom.remove();
		this.view.dom.style.removeProperty('--doc-rail');
	}

	private schedule() {
		this.view.requestMeasure<{ placed: Placed[]; drawn: Element[] }>({
			key: this,
			read: (view) => this.read(view),
			write: (measured) => this.write(measured.placed, measured.drawn),
		});
	}

	private read(view: EditorView) {
		const { state, scrollDOM, contentDOM } = view;
		const scroller = scrollDOM.getBoundingClientRect();
		const origin = view.documentTop - scroller.top + scrollDOM.scrollTop;
		const drawnLines = new Set<Element>();
		// Keyed by the block's start and, inside a rendered table, its row: document order is block start, then row.
		const byLine = new Map<string, { at: [number, number]; top: number; ids: string[]; row: number }>();
		for (const range of state.field(commentRanges)) {
			const block = view.lineBlockAt(state.doc.lineAt(range.from).from);
			// A rendered line is measured where it is drawn; the height map stands in for a line the viewport has not drawn.
			const element = this.drawnLine(view, block);
			// A comment inside a rendered table sits at its row, not at the table's top.
			const inRow = element ? tableRowAt(view, element, range.from) : null;
			const drawn = inRow?.rect ?? element?.getBoundingClientRect();
			const at: [number, number] = [block.from, inRow ? inRow.index + 1 : 0], key = at.join(':');
			if (element) drawnLines.add(element);
			const line = byLine.get(key) ?? {
				at, top: drawn ? drawn.top - scroller.top + scrollDOM.scrollTop : block.top + origin,
				ids: [], row: Math.min(drawn?.height ?? block.height, view.defaultLineHeight),
			};
			line.ids.push(range.id);
			byLine.set(key, line);
		}
		// A block widget that resizes (an image that loads) moves every line below it, commented or not.
		for (const child of contentDOM.children) if (!child.classList.contains('cm-line')) drawnLines.add(child);
		const orphans = new Set(state.field(commentRanges).filter((range) => range.lost).map((range) => range.id));
		const placed = [...byLine.values()].sort((a, b) => a.at[0] - b.at[0] || a.at[1] - b.at[1])
			.map((line): Placed => ({ top: line.top, row: line.row, ids: [...new Set(line.ids)], orphan: line.ids.every((id) => orphans.has(id)) }));
		return { placed, drawn: [...drawnLines] };
	}

	private drawnLine(view: EditorView, { from, widget }: BlockInfo): Element | null {
		if (from < view.viewport.from || from > view.viewport.to) return null;
		// A line drawn as a block widget (an image alone on its line) is no .cm-line, and its height can change without an
		// editor update, as when the image loads: it is the text's child that starts at the line.
		if (widget) return [...view.contentDOM.children].find((child) => !child.classList.contains('cm-line') && view.posAtDOM(child) === from) ?? null;
		const { node } = view.domAtPos(from);
		const line = (node instanceof Element ? node : node.parentElement)?.closest('.cm-line');
		return line && view.contentDOM.contains(line) ? line : null;
	}

	private write(placed: Placed[], drawn: Element[]) {
		this.watch(drawn);
		while (this.lines.length > placed.length) this.lines.pop()!.remove();
		while (this.lines.length < placed.length) {
			const el = document.createElement('div');
			el.className = 'cm-comment-line';
			this.dom.append(el);
			this.lines.push(el);
		}
		placed.forEach((line, index) => {
			const el = this.lines[index];
			el.dataset.commentIds = line.ids.join(',');
			if (line.orphan) el.dataset.orphan = 'true'; else delete el.dataset.orphan;
			el.style.top = `${line.top}px`;
			el.style.height = `${line.row}px`;
		});
	}

	private watch(lines: Element[]) {
		if (!this.resize) return;
		const next = new Set(lines);
		for (const line of this.watched) if (!next.has(line)) this.resize.unobserve(line);
		for (const line of next) if (!this.watched.has(line)) this.resize.observe(line);
		this.watched = next;
	}
}

export const commentRail = [
	commentRanges,
	ViewPlugin.fromClass(CommentRail),
];
