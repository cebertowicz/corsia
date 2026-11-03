import { Transaction } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { NEAR_BOTTOM_PX } from '$lib/nearBottom';

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
	const followAppend = transactions.some(isRemoteAppend)
		&& scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < NEAR_BOTTOM_PX;
	const remoteChange = transactions.some((tr) => tr.docChanged && tr.annotation(Transaction.userEvent) === undefined);
	const place = remoteChange && !followAppend ? readingPlace(view) : null;
	view.update(transactions);
	if (followAppend) {
		heldPlaces.delete(view);
		scroller.scrollTop = scroller.scrollHeight;
		view.lineBlockAtHeight(0);
	} else if (place) {
		// lineBlockAtHeight reads the layout through readMeasured, which runs the pending measure pass now; lineBlockAt
		// returns stored heights without measuring.
		view.lineBlockAtHeight(0);
		holdReadingPlace(view, place, transactions);
	} else if (transactions.some((tr) => tr.docChanged)) heldPlaces.delete(view);
}

/** The line at the top of the reader's view, and where it is on screen, in fractional pixels. */
interface ReadingPlace { from: number; top: number }

/**
 * Where a run of remote changes is holding each editor's top line. The browser keeps a scroll offset in whole (or device)
 * pixels, so the scroll CodeMirror writes for a change above the view lands up to a pixel short; measured from wherever
 * the last change left it, every change of a streaming write adds its own shortfall and the text creeps down the screen.
 * Holding the line at the place the run started keeps the shortfall under a pixel however long the run is.
 */
const heldPlaces = new WeakMap<EditorView, ReadingPlace>();

function readingPlace(view: EditorView): ReadingPlace {
	const block = view.lineBlockAtHeight(view.scrollDOM.getBoundingClientRect().top - view.documentTop);
	const top = view.documentTop + block.top;
	const held = heldPlaces.get(view);
	// The reader has moved since the last change when the held line is not where it was left; their place is the new one.
	return held && held.from === block.from && Math.abs(held.top - top) < 1 ? held : { from: block.from, top };
}

function holdReadingPlace(view: EditorView, place: ReadingPlace, transactions: readonly Transaction[]): void {
	const from = transactions.reduce((pos, tr) => tr.changes.mapPos(pos, 1), place.from);
	const line = view.state.doc.lineAt(from).from;
	const drift = view.documentTop + view.lineBlockAt(line).top - place.top;
	if (Math.abs(drift) >= 0.5) view.scrollDOM.scrollTop += drift;
	heldPlaces.set(view, { from: line, top: place.top });
}

/**
 * Keeps the text in view where it is on screen when the editor's box moves: a notice or the review bar growing or
 * shrinking in the document header above it moves the editor's top edge, and with it every line, by that height. The
 * editor's box grows or shrinks by the same height, which is what the observer sees; the scroll offset moves with the
 * edge in the same frame, before it paints. Only the editor's position on the page counts, so scrolling the page itself
 * moves nothing.
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
	scroller.addEventListener('scroll', onScroll, { passive: true });
	return () => {
		observer.disconnect();
		scroller.removeEventListener('scroll', onScroll);
	};
}
