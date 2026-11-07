import { onDestroy } from 'svelte';
import { fromStore } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import { paneCollapsed } from '$lib/stores/paneCollapsed';

/** What waits for the hold on each text area to end. A hold that replaces another takes its waiters over. */
const waiting = new WeakMap<HTMLElement, Set<() => void>>();

function release(area: HTMLElement | null) {
	const queue = area ? waiting.get(area) : undefined;
	if (!area || !queue) return;
	waiting.delete(area);
	for (const then of queue) then();
}

/** Run `then` once the reading-line hold on `area` has ended, or now when none is running. Returns a cancel. */
export function afterHold(area: HTMLElement, then: () => void): () => void {
	const queue = waiting.get(area);
	if (!queue) { then(); return () => {}; }
	queue.add(then);
	return () => queue.delete(then);
}

/**
 * Keeps the reader's line where it is while the margin column and the shell change the text width.
 * Call it while a component initialises; it ends with the component.
 */
export function holdReadingLine(inputs: { margin: () => boolean; bounds: () => HTMLElement | null; selectedId: () => string | null }) {
	const { margin, bounds, selectedId } = inputs;
	const collapsed = fromStore(paneCollapsed);
	// The reader's top line stays where it is while the column and the shell change the text width, on open and on close.
	// The anchor is re-applied from a ResizeObserver, which runs after layout and before paint in each frame, so every painted frame holds it.
	// The Workspace panel opened or closed by hand while the column is up changes the text width the same way, so it holds a line too: the row of the thread being read, so its card stays beside it (the cards above it stack up from that row), else the top row. 
	let wasMargin = false, wasPane = false, stopHold: ((ended?: boolean) => void) | null = null;
	$effect.pre(() => {
		const now = margin(), pane = collapsed.current;
		if (now === wasMargin && (!now || pane === wasPane)) return;
		const byHand = now && wasMargin;
		wasPane = pane;
		wasMargin = now;
		stopHold?.(false);
		const area = bounds();
		const dom = area?.querySelector<HTMLElement>('.cm-editor');
		const view = dom ? EditorView.findFromDOM(dom) : null;
		if (!area || !view) return release(area);
		const scroller = view.scrollDOM, box = scroller.getBoundingClientRect();
		const x = box.left + box.width / 2;
		const read = byHand && selectedId() ? bounds()?.querySelector(`.cm-comment-line[data-comment-ids*="${selectedId()}"]`)?.getBoundingClientRect().top : undefined;
		let pos = view.posAtCoords({ x, y: read !== undefined && read > box.top && read < box.bottom ? read + 2 : box.top + 4 }, false);
		// A blank line at the top does not rewrap and holds nothing the reader reads; anchor the first text row below it instead.
		const doc = view.state.doc;
		let line = doc.lineAt(pos);
		while (line.length === 0 && line.number < doc.lines) line = doc.line(line.number + 1);
		if (line.from > pos) pos = view.posAtCoords({ x, y: (view.coordsAtPos(line.from)?.top ?? box.top) + 2 }, false);
		// The anchor's row is read from the browser's own layout of its character (a DOM range), not from the editor's height estimates, which can lag a frame while the text rewraps.
		const rowTop = (): number | null => {
			const { node, offset: at } = view.domAtPos(pos);
			if (node.nodeType !== 3) return view.coordsAtPos(pos)?.top ?? null;
			const range = document.createRange();
			range.setStart(node, at); range.setEnd(node, Math.min(at + 1, (node as Text).length));
			const rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
			return rect.height ? rect.top : null;
		};
		const offset = rowTop();
		if (offset === null) return release(area);
		if (!waiting.has(area)) waiting.set(area, new Set());
		const hold = (here: number | null) => { if (here !== null && Math.abs(here - offset) > 0.5) scroller.scrollTop += here - offset; };
		// A width change leaves the editor's height estimates for the lines it is not drawing stale until its next measure, which would be a frame late: measure first, then hold.
		const observer = new ResizeObserver(() => {
			try {
				(view as unknown as { measure: () => void }).measure();
			} catch {
				// Mid-update (or without that internal call), hold from the editor's own next measure instead of from stale estimates.
				view.requestMeasure({ read: rowTop, write: hold });
				return;
			}
			hold(rowTop());
		});
		observer.observe(view.contentDOM);
		observer.observe(scroller);
		const timer = setTimeout(() => stopHold?.(), 1500);
		stopHold = (ended = true) => { observer.disconnect(); clearTimeout(timer); stopHold = null; if (ended) release(area); };
	});
	onDestroy(() => stopHold?.());
}
