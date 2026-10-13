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
		const onTransitionEnd = (event: Event) => {
			if (event.target === node) update();
		};
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
		observer?.observe(node);
		window.addEventListener('resize', update);
		node.addEventListener('transitionend', onTransitionEnd);
		update();

		return () => {
			observer?.disconnect();
			window.removeEventListener('resize', update);
			node.removeEventListener('transitionend', onTransitionEnd);
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
