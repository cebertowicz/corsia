import { onDestroy } from 'svelte';
import { fromStore } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import { paneCollapsed } from '$lib/stores/paneCollapsed';
import { drawnTableAt, tableRowAt, tableRowAtHeight } from '$lib/cm/widget-blocks';
import { placeAnchorAt } from '$lib/documentReadingPlace';

/** The reader's own input, which ends a hold. */
const TAKE_OVER = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const;

/**
 * Keeps the reader's line where it is while the margin column and the shell change the text width.
 * Call it while a component initialises; it ends with the component.
 */
export function holdReadingLine(inputs: { margin: () => boolean; bounds: () => HTMLElement | null; selectedId: () => string | null }) {
	const { margin, bounds, selectedId } = inputs;
	const collapsed = fromStore(paneCollapsed);
	// The reader's top line stays where it is while the column and the shell change the text width, on open and on close.
	// The anchor is re-applied from a ResizeObserver, which runs after layout and before paint in each frame, so every painted frame holds it.
	// The Workspace panel opened or closed by hand while the column is up changes the text width the same way, so it holds a line too.
	// Where a thread is picked (the click on its text that opens the column, or the one being read when the panel moves) the line held
	// is that thread's row, so the text just clicked stays where it was and its card comes beside it (the cards above it stack up from
	// that row); else the top row. Rows between the top and the picked one rewrap taller as the text narrows, and would push it away.
	let wasMargin = false, wasPane = false, stopHold: (() => void) | null = null;
	$effect.pre(() => {
		const now = margin(), pane = collapsed.current;
		if (now === wasMargin && (!now || pane === wasPane)) return;
		wasPane = pane;
		wasMargin = now;
		stopHold?.();
		const area = bounds();
		const dom = area?.querySelector<HTMLElement>('.cm-editor');
		const view = dom ? EditorView.findFromDOM(dom) : null;
		if (!area || !view) return;
		const scroller = view.scrollDOM, box = scroller.getBoundingClientRect();
		const x = box.left + box.width / 2;
		const read = now && selectedId() ? bounds()?.querySelector(`.cm-comment-line[data-comment-ids*="${selectedId()}"]`)?.getBoundingClientRect().top : undefined;
		const picked = read !== undefined && read > box.top && read < box.bottom;
		const y = picked ? read + 2 : box.top + 4;
		let pos = view.posAtCoords({ x, y }, false);
		// A table is one block to the editor and its rows rewrap with the width: anchor a drawn row, not the table's top. The picked
		// thread's row is the one clicked; the top row follows the reader's-place rule the editor's own holder uses, so both aim at one row.
		const table = drawnTableAt(view, pos);
		let row: { pos: number } | null = null;
		if (table && picked) row = tableRowAtHeight(view, table, y);
		else if (table) ({ row, from: pos } = placeAnchorAt(view, box.top, box.bottom));
		if (row) pos = row.pos;
		// A blank line at the top does not rewrap and holds nothing the reader reads; anchor the first text row below it instead.
		const doc = view.state.doc;
		let line = doc.lineAt(pos);
		while (line.length === 0 && line.number < doc.lines) line = doc.line(line.number + 1);
		if (line.from > pos) pos = view.posAtCoords({ x, y: (view.coordsAtPos(line.from)?.top ?? box.top) + 2 }, false);
		// The anchor's row is read from the browser's own layout of its character (a DOM range), not from the editor's height estimates, which can lag a frame while the text rewraps.
		const rowTop = (): number | null => {
			if (row) {
				const drawn = drawnTableAt(view, pos);
				return (drawn && tableRowAt(view, drawn, pos)?.rect.top) ?? null;
			}
			const { node, offset: at } = view.domAtPos(pos);
			if (node.nodeType !== 3) return view.coordsAtPos(pos)?.top ?? null;
			const range = document.createRange();
			range.setStart(node, at); range.setEnd(node, Math.min(at + 1, (node as Text).length));
			const rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
			return rect.height ? rect.top : null;
		};
		const offset = rowTop();
		if (offset === null) return;
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
		// The reader taking over (scrolling, pressing a card, typing) ends the hold, or its next resize would pull them back.
		const takeOver = () => stopHold?.();
		for (const kind of TAKE_OVER) document.addEventListener(kind, takeOver, { capture: true, passive: true });
		stopHold = () => {
			observer.disconnect(); clearTimeout(timer); stopHold = null;
			for (const kind of TAKE_OVER) document.removeEventListener(kind, takeOver, { capture: true });
		};
	});
	onDestroy(() => stopHold?.());
}
