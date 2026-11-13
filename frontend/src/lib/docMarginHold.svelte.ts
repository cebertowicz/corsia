import { onDestroy } from 'svelte';
import { fromStore } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import { isPaneShortcut, paneCollapsed } from '$lib/stores/paneCollapsed';
import { drawnTableAt, tableRowAt, tableRowAtHeight } from '$lib/cm/widget-blocks';
import { placeAnchorAt } from '$lib/documentReadingPlace';
import { commentRanges } from '$lib/cm/comment-marks';

/** The reader's own input, which ends a hold. */
const TAKE_OVER = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const;

/** Whether a width transition is still moving the shell. */
const shellMoving = () => typeof CSSTransition !== 'undefined' && (document.getAnimations?.() ?? [])
	.some((animation) => animation instanceof CSSTransition && animation.transitionProperty === 'width' && animation.playState === 'running');

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
	let wasMargin = false, wasPane = collapsed.current, stopHold: (() => void) | null = null;
	let panelPlace: { view: EditorView; pos: number; offset: number; table: boolean } | null = null;
	$effect.pre(() => {
		const now = margin(), pane = collapsed.current;
		if (now === wasMargin && pane === wasPane) return;
		const returning = pane && !wasPane && now === wasMargin;
		const paneMoved = pane !== wasPane;
		if (!returning) panelPlace = null;
		wasPane = pane;
		wasMargin = now;
		stopHold?.();
		const area = bounds();
		const dom = area?.querySelector<HTMLElement>('.cm-editor');
		const view = dom ? EditorView.findFromDOM(dom) : null;
		if (!area || !view) return;
		const scroller = view.scrollDOM, box = scroller.getBoundingClientRect();
		const x = box.left + box.width / 2;
		// The row to hold is the one the reader is writing about: the text a pending comment quotes, else a picked thread's
		// line. Holding the top line alone let a reflow push that text out of the view the composer sits in: the composer
		// follows its own text, so it ended clamped at the canvas's edge with the word below the fold.
		const subject = bounds()?.querySelector('.cm-comment-pending')
			?? (now && selectedId()
				? bounds()?.querySelector(`.cm-comment-line[data-comment-ids*="${selectedId()}"]`)
				: null);
		// Its row counts while any of it shows: the shell's move can slide the view's top edge over the row's top for a frame,
		// and holding the next whole row instead let the rewrap carry the quoted text up under the toolbar.
		const seen = subject?.getBoundingClientRect();
		const read = seen?.top;
		const picked = read !== undefined && !!seen && seen.bottom > box.top && read < box.bottom;
		const y = picked ? read + 2 : box.top + 4;
		// A picked subject is held by its own text, not by the point on screen: mid-move the editor's map of the screen can lag
		// the drawn text by rows, and the character it named then sat above the view while being held there.
		const pending = picked && subject?.matches('.cm-comment-pending') ? subject : null;
		const thread = picked && !pending ? view.state.field(commentRanges, false)?.find((range) => range.id === selectedId()) : undefined;
		let pos = pending ? view.posAtDOM(pending) : thread ? view.state.doc.lineAt(thread.from).from : view.posAtCoords({ x, y }, false);
		// A table is one block to the editor and its rows rewrap with the width: anchor a drawn row, not the table's top. The picked
		// thread's row is the one clicked; the top row follows the reader's-place rule the editor's own holder uses, so both aim at one row.
		const table = drawnTableAt(view, pos);
		let row: { pos: number } | null = null;
		if (table && picked) row = tableRowAtHeight(view, table, y);
		else if (!picked) {
			const next = placeAnchorAt(view, box.top, box.bottom);
			// If one paragraph fills the view, retain the character at the edge, not its off-screen start.
			if (table || next.from !== view.lineBlockAt(pos).from) pos = next.from;
			row = next.row;
		}
		if (row) pos = row.pos;
		// A blank line at the top does not rewrap and holds nothing the reader reads; anchor the first text row below it instead.
		const doc = view.state.doc;
		let line = doc.lineAt(pos);
		while (line.length === 0 && line.number < doc.lines) line = doc.line(line.number + 1);
		if (line.from > pos) pos = view.posAtCoords({ x, y: (view.coordsAtPos(line.from)?.top ?? box.top) + 2 }, false);
		const returningPlace = returning && panelPlace?.view === view ? panelPlace : null;
		if (returningPlace) {
			pos = returningPlace.pos;
			row = returningPlace.table ? { pos } : null;
		}
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
		// A picked row is held inside the view: by the time the column opens the move may already have carried it above the
		// view's top edge, and holding it where it was measured kept it under the toolbar.
		const measured = rowTop();
		const inView = (top: number | null) => top === null || !picked ? top : Math.min(Math.max(top, box.top), box.bottom - view.defaultLineHeight);
		const offset = returningPlace?.offset ?? inView(measured);
		if (offset === null) return;
		panelPlace = !pane ? { view, pos, offset, table: !!row } : null;
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
			if (paneMoved && !shellMoving()) settle();
		});
		observer.observe(view.contentDOM);
		observer.observe(scroller);
		const timer = setTimeout(() => observer.disconnect(), 1500);
		// A hold the panel started lasts as long as the panel's move: after it, a scroll made by code (an app jump) is not
		// pulled back. The frame the move ends is still held (its resize comes after transitionend), then the next frame lets go.
		let settling = 0;
		const settle = () => { settling ||= requestAnimationFrame(() => observer.disconnect()); };
		const moveEnded = (event: Event) => { if (paneMoved && (event as TransitionEvent).propertyName === 'width' && !shellMoving()) settle(); };
		document.addEventListener('transitionend', moveEnded, true);
		document.addEventListener('transitioncancel', moveEnded, true);
		// Keep the opening anchor for the return unless the reader acts elsewhere in the meantime.
		const takeOver = (event: Event) => {
			// The panel's own control and its shortcut (its modifier goes down first) are the trip, not the reader acting elsewhere.
			const shortcut = isPaneShortcut(event) || (event instanceof KeyboardEvent && (event.key === 'Meta' || event.key === 'Control'));
			if (shortcut || (event.target instanceof Element && event.target.closest('[aria-label="Toggle panel"]'))) return;
			panelPlace = null;
			stopHold?.();
		};
		for (const kind of TAKE_OVER) document.addEventListener(kind, takeOver, { capture: true, passive: true });
		stopHold = () => {
			observer.disconnect(); clearTimeout(timer); cancelAnimationFrame(settling); stopHold = null;
			document.removeEventListener('transitionend', moveEnded, true);
			document.removeEventListener('transitioncancel', moveEnded, true);
			for (const kind of TAKE_OVER) document.removeEventListener(kind, takeOver, { capture: true });
		};
	});
	onDestroy(() => stopHold?.());
}
