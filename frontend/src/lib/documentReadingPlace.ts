import { Transaction, type StateEffectType } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { NEAR_BOTTOM_PX } from '$lib/nearBottom';
import { drawnTableAt, tableRowAt, tableRowAtHeight } from '$lib/cm/widget-blocks';

/**
 * The reader's place: the line at the top of the editor's view and how far below the document pane's top edge that line
 * starts. The pane holds the header row and the editor, so a notice or the review bar growing in the header moves the
 * editor and its text but not this edge, and the place still says where the reader had the line on screen. The line is
 * found and laid through CodeMirror's height map, so it is known for text the editor has not drawn yet. A table is one
 * block to the editor, so inside a drawn table the place is the row at the edge, found and laid from the drawn rows: a
 * row above it growing, shrinking or inserted in the same table then moves the place with the text.
 *
 * `scrollTop` is the offset it was read at; at 0 or less the reader is at the very start, and the place is the start
 * itself, whatever the header does. `end` marks a reader following text appended to the end; the place is the end.
 */
export interface TextDocumentPlace {
	from: number;
	top: number;
	scrollTop: number;
	end?: boolean;
}

/** The edge places are measured from: the document pane's top, or the scroller's for an editor outside one. */
function frameTop(view: EditorView): number {
	return (view.dom.closest<HTMLElement>('.doc') ?? view.scrollDOM).getBoundingClientRect().top;
}

/** How far below the frame edge the line block holding `from` starts, or inside a drawn table the row holding it. */
export function placeTop(view: EditorView, from: number): number {
	const pos = Math.min(from, view.state.doc.length), block = view.lineBlockAt(pos), table = drawnTableAt(view, block);
	const row = table && tableRowAt(view, table, pos);
	return (row ? row.rect.top : view.documentTop + block.top) - frameTop(view);
}

/** Runs CodeMirror's pending measure now, so its heights are the layout's (its own resize handling waits 50 ms). */
function measureNow(view: EditorView): void {
	view.requestMeasure();
	view.lineBlockAtHeight(0);
}

/** Reads the reader's place: the line at the scroller's top edge, at the current offset or at `scrollTop`. */
export function captureDocumentPlace(view: EditorView, scrollTop?: number): TextDocumentPlace {
	measureNow(view);
	const scroller = view.scrollDOM;
	const at = scrollTop ?? scroller.scrollTop;
	// At another offset the text sits lower by the difference.
	const shift = scroller.scrollTop - at;
	const edge = scroller.getBoundingClientRect().top - shift;
	const block = view.lineBlockAtHeight(edge - view.documentTop), table = drawnTableAt(view, block);
	let row = table && tableRowAtHeight(view, table, edge);
	// A row cut off at the edge is context above the reader; the place is the next row, the first one wholly started in view,
	// so that the cut-off row growing or shrinking moves only itself.
	if (row && row.rect.top < edge - 0.5 && row.rect.bottom < scroller.getBoundingClientRect().bottom - shift) row = tableRowAtHeight(view, table!, row.rect.bottom);
	if (row) return { from: row.pos, top: row.rect.top + shift - frameTop(view), scrollTop: at };
	return { from: block.from, top: view.documentTop + shift + block.top - frameTop(view), scrollTop: at };
}

/**
 * Each editor's place to hold, the offset last left by `applyPlace` or by the reader (so its own scroll is not taken for
 * the reader's, and a cut to the end of the range is known for the browser's), and whether a frame is already asked to
 * lay the place. The place is the keeper's own copy: a change moves its line in place,
 * and it stays the same place until something sets another.
 */
interface Keeper {
	target: TextDocumentPlace | null;
	applied: number;
	frame: boolean;
}

const keepers = new WeakMap<EditorView, Keeper>();

function keeperOf(view: EditorView): Keeper {
	let keeper = keepers.get(view);
	if (!keeper) keepers.set(view, keeper = { target: null, applied: Number.NaN, frame: false });
	return keeper;
}

/** The place the editor holds now; null while CodeMirror scrolls somewhere on its own. */
export function heldPlace(view: EditorView): TextDocumentPlace | null {
	return keepers.get(view)?.target ?? null;
}

/** Makes `place` the one the editor holds (a tab return, an image Back) and lays it; returns the place held. */
export function holdPlace(view: EditorView, place: TextDocumentPlace): TextDocumentPlace {
	const held = keeperOf(view).target = { ...place };
	applyPlace(view);
	return held;
}

/**
 * Lets CodeMirror scroll on its own (to the caret, or to a comment's text) and takes the reader's place again from where
 * it leaves them, in a frame registered after its measure. Until then nothing is held, so nothing scrolls back.
 */
export function releasePlace(view: EditorView): void {
	const keeper = keeperOf(view);
	keeper.target = null;
	requestAnimationFrame(() => {
		if (keeper.target === null && view.dom.isConnected) keeper.target = captureDocumentPlace(view);
	});
}

/** Passes over a layout the editor is still measuring: each correction of its estimated heights moves the line. */
const MAX_PASSES = 4;

/**
 * The one writer of a document's scroll offset. Lays the held place where it belongs: the start at 0, the end at the
 * end, a line where the height map puts it less its distance from the frame edge. The offset is worked out whole each
 * time, never added to the one before, so the pixel the browser rounds off one write is not carried into the next.
 * It is clamped to the scroll range and written only when it is half a pixel or more off. Returns whether it moved.
 */
export function applyPlace(view: EditorView): boolean {
	const keeper = keepers.get(view);
	const target = keeper?.target;
	if (!keeper || !target || !view.dom.isConnected) return false;
	const scroller = view.scrollDOM;
	let moved = false;
	for (let pass = 0; pass < MAX_PASSES; pass++) {
		measureNow(view);
		const max = scroller.scrollHeight - scroller.clientHeight;
		const wanted = target.end ? max : target.scrollTop <= 0 ? 0 : scroller.scrollTop + placeTop(view, target.from) - target.top;
		const next = Math.max(0, Math.min(max, wanted));
		if (Math.abs(next - scroller.scrollTop) < 0.5) break;
		scroller.scrollTop = next;
		moved = true;
	}
	keeper.applied = scroller.scrollTop;
	return moved;
}

/**
 * Lays the place in the next frame, before it paints. A layout read inside the dispatching task can include a notice the
 * same task takes away again, which no frame paints and no resize observation reports.
 */
function applyBeforePaint(view: EditorView): void {
	const keeper = keeperOf(view);
	if (keeper.frame) return;
	keeper.frame = true;
	requestAnimationFrame(() => {
		keeper.frame = false;
		applyPlace(view);
	});
}

/** True when a non-user transaction only inserts text at the end of the document, judged by where it changes it. */
function isRemoteAppend(transaction: Transaction): boolean {
	if (!transaction.docChanged || transaction.annotation(Transaction.userEvent) !== undefined) return false;
	const end = transaction.startState.doc.length;
	let append = true;
	transaction.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
		if (fromA !== end || toA !== end || inserted.length === 0) append = false;
	});
	return append;
}

/** The effect type behind `EditorView.scrollIntoView`; CodeMirror exports the constructor but not the type it tests with. */
const SCROLL_INTO_VIEW = (EditorView.scrollIntoView(0) as unknown as { type: StateEffectType<unknown> }).type;

/**
 * Applies the editor's transactions and keeps the reader's place across them. The held line moves with the text the
 * change moves; a change the reader did not make is laid out and the place laid at once, so the scroll CodeMirror makes
 * to keep its own anchor is not taken for the reader's, and laid again before the next paint, so the text in view stays
 * where it is while others edit the document. A remote append follows the live edge when the reader is already as
 * near it as chat considers near. A transaction that scrolls (the reader's typing, a jump to a comment) lets CodeMirror
 * scroll, and the place is taken again from where it lands.
 */
export function dispatchKeepingPlace(transactions: readonly Transaction[], view: EditorView): void {
	const scroller = view.scrollDOM;
	const followAppend = transactions.some(isRemoteAppend)
		&& scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < NEAR_BOTTOM_PX;
	const remoteChange = transactions.some((tr) => tr.docChanged && tr.annotation(Transaction.userEvent) === undefined);
	const scrolls = transactions.some((tr) => tr.scrollIntoView || tr.effects.some((effect) => effect.is(SCROLL_INTO_VIEW)));
	view.update(transactions);
	const keeper = keeperOf(view);
	if (keeper.target && transactions.some((tr) => tr.docChanged)) {
		const from = transactions.reduce((pos, tr) => tr.changes.mapPos(pos, 1), keeper.target.from);
		keeper.target.from = view.state.doc.lineAt(from).from;
	}
	if (scrolls) releasePlace(view);
	else if (followAppend) holdPlace(view, { ...captureDocumentPlace(view), end: true });
	else if (remoteChange) {
		measureNow(view);
		applyPlace(view);
		applyBeforePaint(view);
	}
}

/**
 * Holds the reader's place in this editor until the returned stop is called: takes it at first open and from every
 * scroll of the reader's, and lays it again from the editor's one resize observation, which sees the editor's box move
 * or grow (a notice or the review bar in the header above it) and its text rewrap, after layout and before paint.
 */
export function keepPlaceWhenEditorMoves(view: EditorView): () => void {
	const scroller = view.scrollDOM;
	const keeper = keeperOf(view);
	keeper.target = captureDocumentPlace(view);
	keeper.applied = scroller.scrollTop;
	const onScroll = () => {
		// The editor grew back past the text below the view and the browser cut the offset to the new end: that scroll is
		// the browser's, and the resize observation that follows lays the place again.
		const max = scroller.scrollHeight - scroller.clientHeight;
		if (keeper.applied > max && scroller.scrollTop >= max - 1) return;
		if (Math.abs(scroller.scrollTop - keeper.applied) < 1) return;
		keeper.target = captureDocumentPlace(view);
		keeper.applied = scroller.scrollTop;
	};
	const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => applyPlace(view)) : null;
	observer?.observe(scroller);
	observer?.observe(view.contentDOM);
	scroller.addEventListener('scroll', onScroll, { passive: true });
	return () => {
		observer?.disconnect();
		scroller.removeEventListener('scroll', onScroll);
		keepers.delete(view);
	};
}

/** Reports the place the editor holds at most once a frame while it scrolls, until the returned stop is called. */
export function reportDocumentPlaceOnScroll(view: EditorView, report: (place: TextDocumentPlace) => void): () => void {
	let frame = 0;
	const send = () => {
		frame = 0;
		const held = heldPlace(view);
		report(held ? { ...held } : captureDocumentPlace(view));
	};
	const onScroll = () => { if (!frame) frame = requestAnimationFrame(send); };
	view.scrollDOM.addEventListener('scroll', onScroll, { passive: true });
	return () => {
		view.scrollDOM.removeEventListener('scroll', onScroll);
		if (frame) cancelAnimationFrame(frame);
	};
}
