import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';

/** The grid owns the drawer's maximum; add this measured pinned footer before that cap so the list keeps its normal room when it can. */
export function drawerFooterHeight(node: HTMLElement) {
	const doc = node.closest<HTMLElement>('.doc');
	const update = () => doc?.style.setProperty('--doc-drawer-footer-height', `${node.getBoundingClientRect().height}px`);
	update();
	const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(update);
	observer?.observe(node);
	return { destroy() { observer?.disconnect(); doc?.style.removeProperty('--doc-drawer-footer-height'); } };
}

/**
 * Keeps keyboard focus in the drawer when its footer takes it away: Send now is disabled while it sends and the footer
 * leaves once nothing is left to send, and either drops focus to the page, where Escape would close the document.
 * Focus lost to nowhere from a control that was disabled or removed, or held inside the footer when it goes, moves to
 * `home`, a stable place in the drawer that nothing activates, so a following Enter does nothing and Escape closes
 * only the drawer.
 */
export function footerFocusHome(node: HTMLElement, home: () => HTMLElement | null | undefined) {
	let held = false;
	const land = () => {
		const target = home();
		const active = document.activeElement;
		if (target?.isConnected && (!active || active === document.body)) target.focus({ preventScroll: true });
	};
	const onIn = () => { held = true; };
	const onOut = (event: FocusEvent) => {
		held = false;
		const lost = event.target as HTMLElement;
		if (event.relatedTarget === null && (!lost.isConnected || lost.matches(':disabled'))) queueMicrotask(land);
	};
	node.addEventListener('focusin', onIn);
	node.addEventListener('focusout', onOut);
	return {
		destroy() {
			node.removeEventListener('focusin', onIn);
			node.removeEventListener('focusout', onOut);
			if (held || node.contains(document.activeElement)) queueMicrotask(land);
		},
	};
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
 * Calls `reveal` once a sheet has come up and again whenever it moves or changes size (the keyboard rising or falling under
 * a followKeyboard sheet, its content settling), in the next frame so the layout it reads is current. Returns the stop; with no
 * sheet or nothing to call, there is nothing to watch.
 */
export function onSheetMoved(panel: HTMLElement | null, reveal?: () => void): () => void {
	if (!panel || !reveal) return () => {};
	const viewport = panel.ownerDocument.defaultView?.visualViewport;
	let frame = 0;
	const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(reveal); };
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
export function scrollListToCard(drawer: HTMLElement | undefined, id: string, instant: boolean) {
	const list = drawer?.querySelector<HTMLElement>('.drawer-comments');
	const card = list?.querySelector<HTMLElement>(`[data-comment-id="${id}"]`);
	if (!list || !card) return;
	const top = card.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop - 4;
	list.scrollTo({ top: Math.max(0, top), behavior: instant || get(prefersReducedMotion) ? 'instant' : 'smooth' });
}
