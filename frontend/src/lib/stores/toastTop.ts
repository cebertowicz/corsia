import { writable } from 'svelte/store';

/**
 * Where the toast slot starts: the lowest bottom edge, in viewport pixels, among the surfaces that
 * own the top of the main column (the header and each status banner). Toasts sit under them, so a
 * banner that appears or goes moves the slot with it.
 */
export function createToastTopStore() {
	const store = writable(0);
	const nodes = new Set<HTMLElement>();
	let observer: ResizeObserver | null = null;

	function publish(): void {
		let bottom = 0;
		for (const node of nodes) {
			const rect = node.getBoundingClientRect();
			if (rect.height > 0) bottom = Math.max(bottom, rect.bottom);
		}
		store.set(Math.max(0, Math.round(bottom)));
	}

	function register(node: HTMLElement): () => void {
		nodes.add(node);
		if (nodes.size === 1) {
			window.addEventListener('resize', publish);
			if (typeof ResizeObserver !== 'undefined') observer = new ResizeObserver(publish);
		}
		observer?.observe(node);
		publish();

		return () => {
			observer?.unobserve(node);
			nodes.delete(node);
			if (nodes.size === 0) {
				observer?.disconnect();
				observer = null;
				window.removeEventListener('resize', publish);
			}
			publish();
		};
	}

	return { subscribe: store.subscribe, register };
}

export const toastTop = createToastTopStore();

/** Svelte action for a surface at the top of the main column that toasts must sit under. */
export function toastTopSurface(node: HTMLElement) {
	return { destroy: toastTop.register(node) };
}
