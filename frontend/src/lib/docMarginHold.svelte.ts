import { EditorView } from '@codemirror/view';
import { drawnTableAt, tableRowAtHeight } from '$lib/cm/widget-blocks';
import { holdPlace, placeTop } from '$lib/documentReadingPlace';
import { commentRanges } from '$lib/cm/comment-marks';

/**
 * Makes the row the reader is writing about the editor's held place when the margin column opens or closes or the reader
 * picks another subject or starts a draft, so the text just clicked stays where it was while the column, the panel or the window changes
 * the text width. The editor's place keeper is the one writer of the scroll offset and lays the place on every resize;
 * with nothing picked, the keeper's own place (the reader's top line) is the one held. Call while a component initialises.
 */
export function holdReadingLine(inputs: { margin: () => boolean; bounds: () => HTMLElement | null; subject: () => string | null }) {
	const { margin, bounds, subject } = inputs;
	let was: string | null = null, wasMargin = false, late: string | null = null;
	$effect.pre(() => {
		const now = subject(), column = margin();
		if (now === was && column === wasMargin) return;
		was = now;
		wasMargin = column;
		late = now !== null && !holdSubject(bounds(), now) ? now : null;
	});
	// A new draft's quoted text is marked by the editor's own effect, which runs after this surface's pre-effect: hold it
	// once the mark is drawn.
	$effect(() => {
		const now = subject();
		margin();
		if (late === null || now !== late) return;
		late = null;
		holdSubject(bounds(), now);
	});
}

/** Makes the subject's row the editor's held place; false when its row is not drawn in the view. */
function holdSubject(area: HTMLElement | null, now: string): boolean {
	const dom = area?.querySelector<HTMLElement>('.cm-editor');
	const view = dom ? EditorView.findFromDOM(dom) : null;
	if (!view) return false;
	// The subject is the text a pending comment quotes, else the picked thread's line, held by its own text rather than
	// by a point on screen; its row counts while any of it shows.
	const box = view.scrollDOM.getBoundingClientRect();
	const row = view.dom.querySelector(now === 'draft' ? '.cm-comment-pending' : `.cm-comment-line[data-comment-ids*="${CSS.escape(now)}"]`);
	const seen = row?.getBoundingClientRect();
	if (!row || !seen || seen.bottom <= box.top || seen.top >= box.bottom) return false;
	const thread = now === 'draft' ? undefined : view.state.field(commentRanges, false)?.find((range) => range.id === now);
	let pos = thread ? view.state.doc.lineAt(thread.from).from : view.state.doc.lineAt(view.posAtDOM(row)).from;
	// A table is one block to the editor: hold the drawn row clicked, not the table's top.
	const table = drawnTableAt(view, pos);
	if (table) pos = tableRowAtHeight(view, table, seen.top + 2)?.pos ?? pos;
	// Held inside the view: a row the open has already carried above the view's top edge is held at that edge. Places
	// are measured from the keeper's frame edge (the document pane's top).
	const edge = (view.dom.closest<HTMLElement>('.doc') ?? view.scrollDOM).getBoundingClientRect().top;
	const top = Math.min(Math.max(placeTop(view, pos), box.top - edge), box.bottom - edge - view.defaultLineHeight);
	// A picked row is held even at the very top of the document, where the keeper would otherwise hold the start.
	holdPlace(view, { from: pos, top, scrollTop: Math.max(1, view.scrollDOM.scrollTop) });
	return true;
}
