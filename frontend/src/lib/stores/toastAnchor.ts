import { writable } from 'svelte/store';

/** The column the toast stack centres on, in viewport pixels. */
export interface ToastAnchor {
	x: number;
	width: number;
}

function measureAnchor(node: HTMLElement): ToastAnchor | null {
	const rect = node.getBoundingClientRect();
	const left = Math.max(0, rect.left);
	const right = Math.min(window.innerWidth, rect.right);
	if (right - left <= 0) return null;
	return { x: (left + right) / 2, width: right - left };
}

/**
 * Surfaces register the column they own (the main area, and inside a thread the
 * chat column). The most recently registered surface that is still mounted
 * wins, so the chat column takes over from the main area and hands back when
 * it unmounts. With none, the stack centres on the window.
 */
export function createToastAnchorStore() {
	const store = writable<ToastAnchor | null>(null);
	const surfaces: { node: HTMLElement; anchor: ToastAnchor | null }[] = [];

	function publish(): void {
		for (let i = surfaces.length - 1; i >= 0; i--) {
			const anchor = surfaces[i].anchor;
			if (anchor) {
				store.set(anchor);
				return;
			}
		}
		store.set(null);
	}

	function register(node: HTMLElement): () => void {
		const surface = { node, anchor: null as ToastAnchor | null };
		surfaces.push(surface);
		const update = () => {
			surface.anchor = measureAnchor(node);
			publish();
		};
		// An ancestor that slides or resizes the column (a drawer or pane) moves
		// it without resizing it, which the observer cannot see; re-measure as
		// each such transition settles.
		const onTransitionEnd = (event: Event) => {
			const target = event.target;
			if (target instanceof Node && (target === node || target.contains(node))) update();
		};
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
		observer?.observe(node);
		window.addEventListener('resize', update);
		document.addEventListener('transitionend', onTransitionEnd, true);
		document.addEventListener('transitioncancel', onTransitionEnd, true);
		update();

		return () => {
			observer?.disconnect();
			window.removeEventListener('resize', update);
			document.removeEventListener('transitionend', onTransitionEnd, true);
			document.removeEventListener('transitioncancel', onTransitionEnd, true);
			surfaces.splice(surfaces.indexOf(surface), 1);
			publish();
		};
	}

	return { subscribe: store.subscribe, register };
}

export const toastAnchor = createToastAnchorStore();

export function toastAnchorSurface(node: HTMLElement) {
	return { destroy: toastAnchor.register(node) };
}
