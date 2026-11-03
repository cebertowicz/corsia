import { Transaction } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { NEAR_BOTTOM_PX } from '$lib/nearBottom';

/**
 * The reader's place: the line at the top of the editor's view and how far below the scroller's top edge it starts.
 * It is read against the scroller, not the page, so a header that moves the whole editor leaves it as it was; the
 * offset alone is kept to fall back on. The line is found and measured through CodeMirror's height map, the same
 * lookup its own scroll anchor uses, so it is known for text the editor has not drawn yet and agrees with the line
 * CodeMirror holds across a change.
 */
export interface TextDocumentPlace {
	from: number;
	top: number;
	scrollTop: number;
}

/** How far below the scroller's top edge the line block holding `from` starts. */
export function placeTop(view: EditorView, from: number): number {
	return view.documentTop + view.lineBlockAt(from).top - view.scrollDOM.getBoundingClientRect().top;
}

/** Reads the reader's place: the line at the scroller's top edge. */
export function captureDocumentPlace(view: EditorView): TextDocumentPlace {
	const scroller = view.scrollDOM;
	const { from } = view.lineBlockAtHeight(scroller.getBoundingClientRect().top - view.documentTop);
	return { from, top: placeTop(view, from), scrollTop: scroller.scrollTop };
}

/** Reports the reader's place at most once a frame while the editor scrolls, until the returned stop is called. */
export function reportDocumentPlaceOnScroll(view: EditorView, report: (place: TextDocumentPlace) => void): () => void {
	let frame = 0;
	const capture = () => {
		frame = 0;
		report(captureDocumentPlace(view));
	};
	const onScroll = () => { if (!frame) frame = requestAnimationFrame(capture); };
	view.scrollDOM.addEventListener('scroll', onScroll, { passive: true });
	return () => {
		view.scrollDOM.removeEventListener('scroll', onScroll);
		if (frame) cancelAnimationFrame(frame);
	};
}

/** A restore putting a saved place back (a tab return, an image Back). While one runs it owns the editor's scroll offset. */
export interface PlaceRestore {
	/** Runs from the editor's resize observation, after a layout and before it is painted. */
	layout(): void;
}

const restores = new WeakMap<EditorView, PlaceRestore>();

/**
 * Hands the editor's scroll offset to a restore until the returned release is called; a newer restore takes it over.
 * Meanwhile nothing else here moves the offset: the restore lays its place against the scroller's top edge, so it
 * already answers for a header that moves the editor, and a second correction of the same move would move the text twice.
 */
export function restoreOwnsPlace(view: EditorView, restore: PlaceRestore): () => void {
	restores.set(view, restore);
	heldPlaces.delete(view);
	return () => {
		if (restores.get(view) === restore) restores.delete(view);
	};
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

/**
 * Applies the editor's transactions and lays out at once any change the reader did not make, so the text in view stays
 * where it is while others edit the document. A remote append follows the live edge when the reader is already as near
 * it as chat considers near; every other remote edit keeps the current top-line anchor. CodeMirror keeps the line at the
 * top of the view in place across a change, by that change's height, when it next measures. Until then every update takes
 * that line again from the scroll offset it measured last: a remote change followed by any other update before the next
 * frame (a typing lane's state, comment ranges remapped onto the change) makes it hold whichever line the change moved
 * under that offset, and the text in view moves by the change's height. Measuring right after the change keeps
 * CodeMirror's own correction, the one thing that moves the scroller for it. The reader's own edits keep the default
 * timing, so their caret scrolls as it always has.
 */
export function dispatchKeepingPlace(transactions: readonly Transaction[], view: EditorView): void {
	const scroller = view.scrollDOM;
	// A restore in progress owns the offset; the change is still laid out at once.
	const restoring = restores.has(view);
	const followAppend = !restoring && transactions.some(isRemoteAppend)
		&& scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < NEAR_BOTTOM_PX;
	const remoteChange = transactions.some((tr) => tr.docChanged && tr.annotation(Transaction.userEvent) === undefined);
	const place = remoteChange && !followAppend && !restoring ? readingPlace(view) : null;
	view.update(transactions);
	if (followAppend) {
		scroller.scrollTop = scroller.scrollHeight;
		view.lineBlockAtHeight(0);
	} else if (remoteChange) {
		// lineBlockAtHeight reads the layout through readMeasured, which runs the pending measure pass now; lineBlockAt
		// returns stored heights without measuring.
		view.lineBlockAtHeight(0);
		if (place) {
			holdReadingPlace(view, place, transactions);
			return;
		}
	}
	if (transactions.some((tr) => tr.docChanged)) heldPlaces.delete(view);
}

/**
 * Where a run of remote changes is holding each editor's top line. The browser keeps a scroll offset in whole (or device)
 * pixels, so the scroll CodeMirror writes for a change above the view lands up to a pixel short; measured from wherever
 * the last change left it, every change of a streaming write adds its own shortfall and the text creeps down the screen.
 * Holding the line at the place the run started keeps the shortfall under a pixel however long the run is.
 */
const heldPlaces = new WeakMap<EditorView, TextDocumentPlace>();

function readingPlace(view: EditorView): TextDocumentPlace {
	const place = captureDocumentPlace(view);
	const held = heldPlaces.get(view);
	// The reader has moved since the last change when the held line is not where it was left; their place is the new one.
	return held && held.from === place.from && Math.abs(held.top - place.top) < 1 ? held : place;
}

function holdReadingPlace(view: EditorView, place: TextDocumentPlace, transactions: readonly Transaction[]): void {
	const from = transactions.reduce((pos, tr) => tr.changes.mapPos(pos, 1), place.from);
	const line = view.state.doc.lineAt(from).from;
	const drift = placeTop(view, line) - place.top;
	if (Math.abs(drift) >= 0.5) view.scrollDOM.scrollTop += drift;
	heldPlaces.set(view, { from: line, top: place.top, scrollTop: view.scrollDOM.scrollTop });
}

/**
 * The editor's one resize observation, and the one thing here that moves its scroll offset when its layout changes.
 *
 * It keeps the text in view where it is on screen when the editor's box moves: a notice or the review bar growing or
 * shrinking in the document header above it moves the editor's top edge, and with it every line, by that height. The
 * editor's box grows or shrinks by the same height, which is what the observer sees; the scroll offset moves with the
 * edge in the same frame, before it paints. Only the editor's position on the page counts, so scrolling the page itself
 * moves nothing. While a restore owns the offset the observation is the restore's: the text it watches rewrap is
 * observed here too, and the move is left to it.
 *
 * Near the end of the document the browser moves the offset itself: an editor that grows past the text left below the
 * view has its offset pulled back to the new end at layout, which holds the text by as much as it pulls. Only the rest
 * of the move is the editor's to make, so the distance to the end is kept as of the last scroll. A scroll that comes
 * after the editor moved and before the observer saw it is that pull-back itself, and leaves the distance as it was.
 */
export function keepPlaceWhenEditorMoves(view: EditorView): () => void {
	if (typeof ResizeObserver !== 'function') return () => {};
	const scroller = view.scrollDOM;
	const pageTop = () => scroller.getBoundingClientRect().top - document.documentElement.getBoundingClientRect().top;
	const toEnd = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
	let top = pageTop();
	let end = toEnd();
	const onScroll = () => {
		if (Math.abs(pageTop() - top) < 0.5) end = toEnd();
	};
	// A header that eases open moves the edge by a fraction each frame, and the browser keeps whole pixels of each
	// scroll; the part it dropped is carried into the next move so the frames add up to the edge's whole move.
	let unscrolled = 0;
	const observer = new ResizeObserver(() => {
		const now = pageTop();
		let moved = now - top + unscrolled;
		top = now;
		unscrolled = 0;
		const restore = restores.get(view);
		if (restore) {
			restore.layout();
			end = toEnd();
			return;
		}
		// The editor grew upward with less text below the view than it grew by: the browser has already scrolled back.
		if (moved < 0 && toEnd() < 0.5) moved = Math.min(0, moved + Math.max(0, -moved - end));
		if (Math.abs(moved) < 0.5) {
			unscrolled = moved;
			end = toEnd();
			return;
		}
		const before = scroller.scrollTop;
		scroller.scrollTop = before + moved;
		unscrolled = Math.abs(moved - (scroller.scrollTop - before)) < 1 ? moved - (scroller.scrollTop - before) : 0;
		end = toEnd();
		heldPlaces.delete(view);
	});
	observer.observe(scroller);
	observer.observe(view.contentDOM);
	scroller.addEventListener('scroll', onScroll, { passive: true });
	return () => {
		observer.disconnect();
		scroller.removeEventListener('scroll', onScroll);
	};
}
