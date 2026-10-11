import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { commentRanges, type CommentRange } from './comment-marks';
import { clusterMarkerLines, railWidth, type MarkerCluster } from './comment-clusters';

const SVG = 'http://www.w3.org/2000/svg';
const GLYPH = 18;

type Opener = NonNullable<CommentRange['onMarkerFocus']>;

interface Placed extends MarkerCluster {
	onFocus?: Opener;
	replyWaiting: boolean;
	/** Height of one normal row, to seat the glyph on the first row. */
	row: number;
}

interface Marker {
	button: HTMLButtonElement;
	ids: string[];
	onFocus?: Opener;
}

function glyph(replyWaiting = false): SVGSVGElement {
	const svg = document.createElementNS(SVG, 'svg');
	svg.setAttribute('width', String(GLYPH));
	svg.setAttribute('height', String(GLYPH));
	svg.setAttribute('viewBox', '0 0 24 24');
	svg.setAttribute('fill', 'none');
	svg.setAttribute('stroke', 'currentColor');
	svg.setAttribute('stroke-width', '2');
	svg.setAttribute('stroke-linecap', 'round');
	svg.setAttribute('stroke-linejoin', 'round');
	svg.setAttribute('aria-hidden', 'true');
	const path = document.createElementNS(SVG, 'path');
	path.setAttribute('d', 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z');
	svg.append(path);
	if (replyWaiting) {
		const dot = document.createElementNS(SVG, 'circle');
		dot.setAttribute('cx', '17');
		dot.setAttribute('cy', '7');
		dot.setAttribute('r', '3');
		dot.setAttribute('fill', 'currentColor');
		svg.append(dot);
	}
	return svg;
}

function fill(button: HTMLButtonElement, ids: string[], replyWaiting: boolean) {
	button.dataset.commentIds = ids.join(',');
	button.setAttribute('aria-label', ids.length === 1 ? 'Open comment' : `Open ${ids.length} comments`);
	if (ids.length === 1) {
		button.replaceChildren(glyph(replyWaiting));
		return;
	}
	const count = document.createElement('span');
	count.className = 'cm-comment-count';
	count.textContent = ids.length > 99 ? '99+' : String(ids.length);
	if (!replyWaiting) {
		button.replaceChildren(count);
		return;
	}
	// The same dot the single glyph carries, on the count box's top-right corner.
	const dot = document.createElement('span');
	dot.className = 'cm-comment-dot';
	dot.setAttribute('aria-hidden', 'true');
	button.replaceChildren(count, dot);
}

/**
 * A rail of margin markers down the left of the text, in the content padding the theme reserves for it. It is
 * not a CodeMirror gutter. Markers sit on the first row of their line (`lineBlockAt`), are regrouped whenever
 * the viewport or geometry changes, and keep their buttons across a regroup so focus and the drawer's opener
 * stay valid.
 */
class CommentRail {
	private readonly dom: HTMLElement;
	private markers: Marker[] = [];
	private readonly resize?: ResizeObserver;
	private watched = new Set<Element>();
	/** The thread the focused button was on when focus reached it; it outlives regroups that change the button's first id. */
	private focusButton?: HTMLButtonElement;
	private focusThread?: string;

	constructor(private readonly view: EditorView) {
		this.dom = document.createElement('div');
		this.dom.className = 'cm-comment-rail';
		view.scrollDOM.append(this.dom);
		// The content padding reserves the rail through --doc-rail; set it here so an editor mounted outside the viewer
		// reserves the same width the viewer's header takes.
		view.dom.style.setProperty('--doc-rail', `${railWidth()}px`);
		// A text size or line height change moves lines without a document or viewport change: regroup on any resize of the
		// text, or of a commented line (the text's own height can be pinned by its minimum height while its lines change).
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
		this.view.requestMeasure<{ placed: Placed[]; left: number; target: number; inset: number; drawn: Element[] }>({
			key: this,
			read: (view) => this.read(view),
			write: (measured) => this.write(measured.placed, measured.left, measured.target, measured.inset, measured.drawn),
		});
	}

	private read(view: EditorView) {
		const { state, scrollDOM, contentDOM } = view;
		// The inset is the text's left padding; a coarse pointer's hit box (railWidth) may be wider than it, reaching into the text's edge.
		const inset = Number.parseFloat(getComputedStyle(contentDOM).paddingLeft) || railWidth();
		const target = Math.max(inset, railWidth());
		const scroller = scrollDOM.getBoundingClientRect();
		const origin = view.documentTop - scroller.top + scrollDOM.scrollTop;
		const drawnLines = new Set<Element>();
		const byLine = new Map<number, { top: number; ids: string[]; onFocus?: Opener; replyWaiting: boolean; row: number }>();
		for (const range of state.field(commentRanges)) {
			const block = view.lineBlockAt(state.doc.lineAt(range.from).from);
			// A rendered line is measured where it is drawn; the height map stands in for a line the viewport has not drawn.
			const element = this.drawnLine(view, block.from);
			const drawn = element?.getBoundingClientRect();
			if (element) drawnLines.add(element);
			const line = byLine.get(block.from) ?? {
				top: drawn ? drawn.top - scroller.top + scrollDOM.scrollTop : block.top + origin,
				ids: [], onFocus: range.onMarkerFocus, replyWaiting: false, row: Math.min(drawn?.height ?? block.height, view.defaultLineHeight),
			};
			line.ids.push(range.id);
			line.replyWaiting ||= !!range.replyWaiting;
			byLine.set(block.from, line);
		}
		const lines = [...byLine.entries()].sort(([a], [b]) => a - b).map(([, line]) => line);
		const placed = clusterMarkerLines(lines, target).map((cluster): Placed => {
			const first = lines.find((line) => line.ids.includes(cluster.ids[0]))!;
			return {
				...cluster, onFocus: first.onFocus, row: first.row,
				replyWaiting: cluster.ids.some((id) => lines.find((line) => line.ids.includes(id))?.replyWaiting),
			};
		});
		const content = contentDOM.getBoundingClientRect();
		return { placed, left: content.left - scroller.left + scrollDOM.scrollLeft, target, inset, drawn: [...drawnLines] };
	}

	private drawnLine(view: EditorView, from: number): Element | null {
		if (from < view.viewport.from || from > view.viewport.to) return null;
		const { node } = view.domAtPos(from);
		const line = (node instanceof Element ? node : node.parentElement)?.closest('.cm-line');
		return line && view.contentDOM.contains(line) ? line : null;
	}

	private write(placed: Placed[], left: number, target: number, inset: number, drawn: Element[]) {
		this.watch(drawn);
		const active = this.dom.ownerDocument.activeElement;
		const held = this.markers.find((marker) => marker.button === active);
		const unclaimed = new Set(this.markers);
		if (held && held.button !== this.focusButton) { this.focusButton = held.button; this.focusThread = held.ids[0]; }
		if (!held) { this.focusButton = undefined; this.focusThread = undefined; }
		const focusId = this.focusThread;
		// The focused button goes to the cluster that holds the thread it was focused on, whatever order the clusters come in.
		const focusIndex = held && focusId ? placed.findIndex((cluster) => cluster.ids.includes(focusId)) : -1;
		if (held && focusIndex >= 0) unclaimed.delete(held);
		const next = placed.map((cluster, index) => {
			// Reuse the button whose threads overlap this cluster most, so a regroup leaves focus and any opener in place.
			let best: Marker | undefined = index === focusIndex ? held : undefined;
			let bestOverlap = best ? 1 : 0;
			for (const marker of best ? [] : unclaimed) {
				const overlap = marker.ids.filter((id) => cluster.ids.includes(id)).length;
				if (overlap > bestOverlap) { best = marker; bestOverlap = overlap; }
			}
			if (best) unclaimed.delete(best);
			const marker = best ?? this.create();
			if (marker.ids.join(',') !== cluster.ids.join(',') || marker.button.dataset.replyWaiting !== String(cluster.replyWaiting)) {
				fill(marker.button, cluster.ids, cluster.replyWaiting);
			}
			marker.button.dataset.replyWaiting = String(cluster.replyWaiting);
			marker.ids = cluster.ids;
			marker.onFocus = cluster.onFocus;
			return { marker, cluster };
		});
		for (const marker of unclaimed) marker.button.remove();
		this.markers = next.map(({ marker }) => marker);
		let after: Element | null = null;
		for (const { marker, cluster } of next) {
			const { style } = marker.button;
			style.left = `${left}px`;
			style.width = `${target}px`;
			style.height = `${target}px`;
			style.top = `${cluster.top}px`;
			style.paddingTop = `${Math.max(0, (cluster.row - GLYPH) / 2)}px`;
			// The glyph is centred in the inset beside the text, not in a hit box that reaches over the text.
			style.paddingRight = `${target - inset}px`;
			const sibling: ChildNode | null = after ? after.nextSibling : this.dom.firstChild;
			if (sibling !== marker.button) this.dom.insertBefore(marker.button, sibling);
			after = marker.button;
		}
		// Moving a focused node in the DOM blurs it: put focus back on the button that held it, where it now stands.
		if (held?.button.isConnected && this.dom.ownerDocument.activeElement !== held.button) held.button.focus({ preventScroll: true });
		// No cluster holds the focused thread any more: focus goes back to the editor, after this measure cycle.
		if (held && !held.button.isConnected) this.dom.ownerDocument.defaultView?.setTimeout(() => { if (this.view.dom.isConnected) this.view.focus(); });
	}

	private watch(lines: Element[]) {
		if (!this.resize) return;
		const next = new Set(lines);
		for (const line of this.watched) if (!next.has(line)) this.resize.unobserve(line);
		for (const line of next) if (!this.watched.has(line)) this.resize.observe(line);
		this.watched = next;
	}

	private create(): Marker {
		const button = document.createElement('button');
		button.type = 'button';
		button.className = 'cm-comment-marker press-tint';
		const marker: Marker = { button, ids: [] };
		button.addEventListener('click', () => marker.onFocus?.(marker.ids, button));
		return marker;
	}
}

export const commentRail = [
	commentRanges,
	ViewPlugin.fromClass(CommentRail),
];
