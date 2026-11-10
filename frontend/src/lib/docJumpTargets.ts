import { EditorView } from '@codemirror/view';
import { clean, snippet } from '$lib/cm/widget-cells';
import { plainQuote } from '$lib/plainQuote';
import { unreadCommentCount } from '$lib/docUnread';
import type { DocComment } from '$lib/stores/docComments';

/** How an unread thread is named in the control's accessible name: its quote with marks out, whitespace flattened, cut
 * like a card's label and set off in curly quotes; a comment on an image has no quote, so its pin numbers it. */
export const jumpTargetLabel = (thread: Pick<DocComment, 'anchor' | 'regionNumber'>) => thread.anchor
	? `“${snippet(clean(plainQuote(thread.anchor)))}”`
	: thread.regionNumber != null ? `pin ${thread.regionNumber}` : 'a comment with no text to mark';

/** One off-screen unread thread: what it is named as, and how far its text is past the edge it is on. */
export type DocJumpTarget = { id: string; label: string; distance: number };

/** What the control shows: the unread total, and the one thread to point at — the nearer side, above on an even draw. */
export type DocJumpTargets = { count: number; target: DocJumpTarget | null; direction: 'above' | 'below' | null };

/** Which box decides whether a landing is on screen: the editor's scrollport, the margin column that clips its own
 * cards, or the frame (the viewport of an image open over the document, which pans either way and clips on all four
 * sides). */
type View = 'text' | 'column' | 'frame';
type Box = { top: number; bottom: number; left: number; right: number };

/** Where an unread thread is drawn, if anywhere: its painted pieces, or the one card or pin that stands for it. */
type Landing = { elements: HTMLElement[]; view: View };

const nearer = <T extends { distance: number }>(nearest: T | null, next: T) => (!nearest || next.distance < nearest.distance ? next : nearest);

/** A place in the document drawn at all: an empty box means nothing was painted there. */
const drawn = (rect: DOMRect) => rect.width > 0 || rect.height > 0;

/**
 * Every landing in the canvas, in one pass, keyed by the thread it belongs to. A quote across several lines paints
 * several pieces; a card stands for a thread whose text is gone; a pin tag stands for a comment on an image.
 */
function landingsById(root: ParentNode): Map<string, Landing> {
	const found = new Map<string, Landing>();
	/** The highlight a thread paints is where it is; a card or a pin stands in only where there is no highlight. */
	const rank: Record<View, number> = { text: 0, column: 1, frame: 2 };
	const put = (id: string, landing: Landing) => {
		const current = found.get(id);
		if (!current || rank[landing.view] < rank[current.view]) found.set(id, landing);
		else if (current.view === landing.view) current.elements.push(landing.elements[0]);
	};
	for (const el of root.querySelectorAll<HTMLElement>('[data-comment-ids], [data-margin-card], [data-region-tag][data-comment-id]')) {
		// An image opened over the document paints the same comments again in its own surface. When the read is the
		// document's, only what the document itself paints decides whether there is somewhere to take the reader: a pin
		// visible in the image says nothing about the text the reader is looking at. When the read belongs to the
		// viewer, that same copy is the only thing there, so it counts.
		const surface = el.closest('.image-overlay');
		if (surface && !surface.contains(root)) continue;
		if (el.dataset.marginCard) {
			if (el.matches('.margin-card.placed')) put(el.dataset.marginCard, { elements: [el], view: 'column' });
			continue;
		}
		if (el.dataset.regionTag) {
			if (el.dataset.commentId) put(el.dataset.commentId, { elements: [el], view: 'frame' });
			continue;
		}
		for (const id of el.dataset.commentIds?.split(',').filter(Boolean) ?? []) put(id, { elements: [el], view: 'text' });
	}
	return found;
}

/** The text offset at the top of the scrollport, or null when no editor answers there. */
function offsetAtTop(scroller: HTMLElement | null, top: number): number | null {
	const content = scroller?.querySelector<HTMLElement>('.cm-content');
	const editor = content ? EditorView.findFromDOM(content) : null;
	if (!editor || !scroller) return null;
	const at = editor.posAtCoords({ x: scroller.getBoundingClientRect().left + 1, y: top + 1 });
	return typeof at === 'number' && Number.isFinite(at) ? at : null;
}

/**
 * What the document's jump control points at: the nearest unread thread with no line of its text on screen, either side
 * of the editor's scrollport, beside the unread total. A thread whose text is on screen is not a target — its own New
 * marker shows it — and becomes one again once it scrolls out. A thread with nothing drawn yet is a target too, since a
 * tap pages and scrolls to it; with nothing drawn either side it sits on whichever side of the top line its text
 * belongs, and below when the editor cannot say.
 */
export function docJumpTargets(root: HTMLElement, threads: DocComment[]): DocJumpTargets {
	const count = unreadCommentCount(threads);
	if (!count) return { count, target: null, direction: null };
	const scroller = root.querySelector<HTMLElement>('.cm-scroller');
	const column = root.querySelector<HTMLElement>('.drawer-comments');
	const views: Record<View, Box> = {
		text: (scroller ?? root).getBoundingClientRect(),
		column: (column ?? scroller ?? root).getBoundingClientRect(),
		frame: (root.querySelector<HTMLElement>('.viewport') ?? root).getBoundingClientRect(),
	};
	const landings = landingsById(root);
	let above: DocJumpTarget | null = null;
	let below: DocJumpTarget | null = null;
	let undrawn: { target: DocJumpTarget; from: number | undefined } | null = null;
	for (const thread of threads) {
		const landing = landings.get(thread.id);
		const rects = landing?.elements.map((el) => el.getBoundingClientRect()).filter(drawn);
		if (!landing || !rects?.length) {
			undrawn ??= { target: { ...threadTarget(thread), distance: 0 }, from: thread.from };
			continue;
		}
		const view = views[landing.view];
		const top = Math.min(...rects.map((rect) => rect.top));
		const bottom = Math.max(...rects.map((rect) => rect.bottom));
		if (bottom <= view.top) above = nearer(above, { ...threadTarget(thread), distance: view.top - bottom });
		else if (top >= view.bottom) below = nearer(below, { ...threadTarget(thread), distance: top - view.bottom });
		else if (landing.view === 'frame') {
			// A pin panned off sideways is off screen too; the arrow points to the half of the frame it sits level with.
			const left = Math.min(...rects.map((rect) => rect.left));
			const right = Math.max(...rects.map((rect) => rect.right));
			const distance = right <= view.left ? view.left - right : left >= view.right ? left - view.right : null;
			if (distance === null) continue;
			const side = { ...threadTarget(thread), distance };
			if (top + bottom < view.top + view.bottom) above = nearer(above, side);
			else below = nearer(below, side);
		}
	}
	if (undrawn && !above && !below) {
		const at = offsetAtTop(scroller, views.text.top);
		if (at !== null && undrawn.from !== undefined && undrawn.from < at) above = undrawn.target;
		else below = undrawn.target;
	}
	const direction: 'above' | 'below' | null = above && (!below || above.distance <= below.distance) ? 'above' : below ? 'below' : null;
	return { count, target: direction === 'above' ? above : below, direction };
}

const threadTarget = (thread: DocComment) => ({ id: thread.id, label: jumpTargetLabel(thread) });

/** The thread a tap goes to, by the target's id, out of the same list the targets were read from. */
export const jumpedThread = (threads: DocComment[], target: DocJumpTarget | null) =>
	target ? threads.find((thread) => thread.id === target.id) ?? null : null;
