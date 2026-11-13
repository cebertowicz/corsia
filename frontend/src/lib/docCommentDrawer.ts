import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';
import type { DocComment } from '$lib/stores/docCommentProjection';
import { pendingSuggestion } from '$lib/docCanvasComments';

/** Image comments follow their tag numbers. */
export const byCommentNumber = (a: DocComment, b: DocComment) => (a.regionNumber ?? Infinity) - (b.regionNumber ?? Infinity);

/** An image drawer follows the viewer's grid track; a document drawer appears at once. */
export function imageDrawerMotion(_node: Element, { leaving }: { leaving: boolean }, isImage: boolean) {
	if (!isImage) return { duration: 0 };
	const token = (name: string) => Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
	const pace = leaving ? 0.7 : 1;
	if (get(prefersReducedMotion)) return { duration: token('--dur-tight') * pace, css: (t: number) => `opacity: ${t}` };
	return { duration: leaving ? token('--dur-drawer') * pace : 0 };
}

/** Authors with at least two open, placed suggestions whose text is unchanged can accept them together. */
export function authorsWithOpenSuggestions(comments: DocComment[]) {
	return Object.entries(comments.filter((comment) => pendingSuggestion(comment) && !comment.orphaned && !comment.textChanged)
		.reduce<Record<string, DocComment[]>>((groups, comment) => ({ ...groups, [comment.author]: [...(groups[comment.author] ?? []), comment] }), {}))
		.filter(([, suggestions]) => suggestions.length >= 2);
}

export const COMMENT_FILTERS = [['open', 'Open'], ['resolved', 'Resolved'], ['all', 'All']] as const;

/** Observe the canvas width so document drawers can choose the sheet layout. */
export function observeCanvasWidth(bounds: HTMLElement | null, update: (width: number) => void) {
	if (!bounds || typeof ResizeObserver === 'undefined') return;
	const read = () => update(bounds.getBoundingClientRect().width);
	read(); const observer = new ResizeObserver(read); observer.observe(bounds);
	return () => observer.disconnect();
}

/** The card or image thumbnail a click in the drawer landed on, if any: what may open an image over the document. */
export function clickedOpener(event: MouseEvent): Element | null {
	return event.target instanceof Element ? event.target.closest('[data-comment-id], [data-region-thumb]') : null;
}

/**
 * Where focus goes back to in the drawer once the image that `opener` opened over the document closes: the element
 * that had focus as it opened, when that is inside `opener`, or else the opener's own first control (a region card's
 * number, which shows it on the image again; a thumbnail is its own button). Null when the opener has gone.
 */
export function openerFocus(opener: Element | null, active: Element | null): HTMLElement | null {
	if (!opener?.isConnected) return null;
	if (active instanceof HTMLElement && opener.contains(active)) return active;
	const control = 'button:not(:disabled)';
	return opener.matches(control) ? opener as HTMLElement : opener.querySelector<HTMLElement>(control);
}

export function drawerKeys(element: HTMLElement, onClose: () => void) {
	function onKey(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		event.stopPropagation();
		onClose();
	}
	element.addEventListener('keydown', onKey);
	return { destroy() { element.removeEventListener('keydown', onKey); } };
}

/**
 * Reports whether any element using the returned action is on screen: true when the first one mounts, false once the
 * last has left. Svelte tears an action down only after its element's outro ends, so a leaving element still counts.
 */
export function presence(report: (shown: boolean) => void) {
	let mounted = 0;
	return (_node: Element) => {
		if (mounted++ === 0) report(true);
		return { destroy() { if (--mounted === 0) report(false); } };
	};
}

/**
 * Calls `reveal(true)` once a sheet has come up and `reveal(false)` whenever it then moves or changes size (the keyboard rising or
 * falling under a followKeyboard sheet, its content settling), in the next frame so the layout it reads is current. The caller decides
 * what a movement needs: it must not undo a scroll the reader chose. Returns the stop; with no sheet or nothing to call, there is
 * nothing to watch.
 */
export function onSheetMoved(panel: HTMLElement | null, reveal?: (opening: boolean) => void): () => void {
	if (!panel || !reveal) return () => {};
	const viewport = panel.ownerDocument.defaultView?.visualViewport;
	let frame = 0, opening = true;
	const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { reveal(opening); opening = false; }); };
	const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(schedule);
	observer?.observe(panel);
	viewport?.addEventListener('resize', schedule);
	viewport?.addEventListener('scroll', schedule);
	schedule();
	return () => {
		cancelAnimationFrame(frame);
		observer?.disconnect();
		viewport?.removeEventListener('resize', schedule);
		viewport?.removeEventListener('scroll', schedule);
	};
}

/** Brings a card into view in the drawer's own list: smooth, or at once when asked or with reduced motion. */
export function scrollListToCard(drawer: HTMLElement | undefined, id: string, instant: boolean, candidateOrder = false) {
	const list = drawer?.querySelector<HTMLElement>('.drawer-comments');
	const card = list?.querySelector<HTMLElement>(`[data-comment-id="${id}"]`);
	if (!list || !card) return;
	const cardRect = card.getBoundingClientRect(), listRect = list.getBoundingClientRect();
	// A visible candidate stays in place when chosen; another pick can still bring an offscreen card into view.
	if (!instant && candidateOrder && cardRect.top >= listRect.top && cardRect.bottom <= listRect.bottom) return;
	const top = cardRect.top - listRect.top + list.scrollTop - 4;
	list.scrollTo({ top: Math.max(0, top), behavior: instant || get(prefersReducedMotion) ? 'instant' : 'smooth' });
}
