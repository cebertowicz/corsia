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

/** Report focus in a sheet's comment input, excluding its own review controls. */
export function observeSheetTyping(panel: HTMLElement | null, update: (typing: boolean) => void) {
	if (!panel) { update(false); return; }
	const isField = (target: EventTarget | null) => target instanceof HTMLElement && panel.contains(target)
		&& !target.closest('.rb-sheet-host') && target.matches('textarea, input, [contenteditable="true"]');
	const onIn = (event: FocusEvent) => update(isField(event.target));
	const onOut = (event: FocusEvent) => update(isField(event.relatedTarget));
	panel.addEventListener('focusin', onIn); panel.addEventListener('focusout', onOut);
	return () => { panel.removeEventListener('focusin', onIn); panel.removeEventListener('focusout', onOut); };
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

/** The least height of document the reader keeps above a half-height sheet, so the highlighted text and a line or two around it stay in view. */
export const HALF_SHEET_MIN_TEXT = 120;
/** Under this much room (the document pane's height) a half-height sheet sizes to its whole card before it keeps any text above it. */
export const SHORT_SCREEN = 500;

/**
 * Lets a half-height comment sheet grow to fit the open card: its content height, up to the room that leaves `HALF_SHEET_MIN_TEXT` of
 * document under the document's top edge. The sheet never gets shorter than its usual half height (that is a CSS floor), and the
 * list scrolls inside it only when the card alone is taller than the room. Sets `--drawer-half-fit` on the panel; returns the stop.
 */
export function fitHalfSheet(panel: HTMLElement | null, textTop: () => number | null): () => void {
	if (!panel) return () => {};
	const list = () => panel.querySelector<HTMLElement>('.drawer-comments');
	let frame = 0;
	const fit = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(() => {
			const body = list(), backdrop = panel.closest<HTMLElement>('[data-adaptive-drawer]'), top = textTop();
			if (!body || !backdrop || top === null) return;
			// Chrome (grabber, heading) plus the list's full content: what the sheet would be with no cap.
			const natural = panel.offsetHeight - body.clientHeight + body.scrollHeight;
			// On a short screen (a phone on its side) there is no room for text above a card that is whole: the card's head and its reply field are both in view first, and the text keeps what is left.
			const room = backdrop.getBoundingClientRect().bottom - (parseFloat(getComputedStyle(panel).marginBottom) || 0) - top - (backdrop.clientHeight < SHORT_SCREEN ? 0 : HALF_SHEET_MIN_TEXT);
			panel.style.setProperty('--drawer-half-fit', `${Math.max(0, Math.floor(Math.min(natural, room)))}px`);
		});
	};
	const resize = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(fit);
	const watch = () => { resize?.disconnect(); resize?.observe(panel); const body = list(); if (body) { resize?.observe(body); for (const child of Array.from(body.querySelectorAll('[data-comment-id]'))) resize?.observe(child); } };
	const mutations = typeof MutationObserver === 'undefined' ? undefined : new MutationObserver(() => { watch(); fit(); });
	mutations?.observe(panel, { childList: true, subtree: true });
	watch();
	fit();
	return () => { cancelAnimationFrame(frame); resize?.disconnect(); mutations?.disconnect(); panel.style.removeProperty('--drawer-half-fit'); };
}

/** The air kept between the bottom of the text a comment is on and the top of the sheet that shows it. */
export const ANCHOR_GAP = 16;

/**
 * Keeps the text a comment is on above the sheet that shows it. A sheet opened on one comment (a tap on its highlight) is as tall as the
 * phone allows, which can put its top over that text: this sets `--drawer-cap` on the panel to the room under the text's bottom edge and
 * the gap, which the panel's max-height reads (never below its half-height floor, so a card always has room to work in). The text moves
 * when the editor scrolls it into view above the sheet, and each move re-reads the room. With no text to protect the cap is removed.
 * Returns the stop.
 */
export function capSheetBelowAnchor(panel: HTMLElement | null, anchorBottom: () => number | null): () => void {
	if (!panel) return () => {};
	let frame = 0;
	const apply = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(() => {
			const backdrop = panel.closest<HTMLElement>('[data-adaptive-drawer]'), bottom = anchorBottom();
			if (!backdrop || bottom === null) { panel.style.removeProperty('--drawer-cap'); return; }
			panel.style.setProperty('--drawer-cap', `${Math.max(0, Math.floor(backdrop.getBoundingClientRect().bottom - bottom - ANCHOR_GAP))}px`);
		});
	};
	const view = panel.ownerDocument.defaultView, viewport = view?.visualViewport;
	const resize = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(apply);
	resize?.observe(panel);
	view?.addEventListener('resize', apply);
	viewport?.addEventListener('resize', apply);
	apply();
	return () => { cancelAnimationFrame(frame); resize?.disconnect(); view?.removeEventListener('resize', apply); viewport?.removeEventListener('resize', apply); panel.style.removeProperty('--drawer-cap'); };
}

/**
 * A card opened under its highlight that the room there cannot hold whole (a short screen, or a highlight low in the viewport): scrolls the
 * document up under it, no further than keeps the highlight in view or the document can go, so the card's reply and who gets it show at rest.
 * Does nothing when the card is already as tall as it wants to be, or sits above its highlight. Returns the distance scrolled.
 */
export function scrollDocumentForCard(card: HTMLElement | null, anchor: DOMRect | null, bounds: HTMLElement | null): number {
	const scroller = bounds?.querySelector<HTMLElement>('.cm-scroller');
	if (!card || !anchor || !scroller) return 0;
	// Placement runs again as typing grows the card; the document moves for the card at open, never under a reply being typed.
	if (card.querySelector<HTMLTextAreaElement>('textarea.ct-input')?.value) return 0;
	const view = card.ownerDocument.defaultView, edge = card.offsetHeight - card.clientHeight;
	const wanted = Math.min(card.scrollHeight + edge, (view?.innerHeight ?? 0) * 0.7, 620);
	const short = Math.ceil(wanted - card.offsetHeight);
	if (short < 2 || card.getBoundingClientRect().top < anchor.top) return 0;
	const by = Math.floor(Math.min(short, anchor.top - scroller.getBoundingClientRect().top - ANCHOR_GAP / 2, scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop));
	if (by < 1) return 0;
	scroller.scrollTop += by;
	return by;
}

const CLIPPING = ['auto', 'scroll', 'hidden', 'clip'];

/** The top edge of the part of the document a reader can see: the lowest top among the editor's clipping ancestors. */
export function editorTop(bounds: HTMLElement | null): number | null {
	const start = bounds?.querySelector('.cm-content');
	if (!start) return null;
	let top = 0;
	for (let element = start.parentElement; element; element = element.parentElement) {
		if (CLIPPING.includes(getComputedStyle(element).overflowY)) top = Math.max(top, element.getBoundingClientRect().top);
	}
	return top;
}
