import { writable } from 'svelte/store';

export type ToastClearanceMeasurement = () => number;

interface ToastClearanceRegistration {
	clearance: number;
	observer: ResizeObserver | null;
	update: () => void;
}

function viewportBottomOccupancy(node: HTMLElement): number {
	const { top, bottom } = node.getBoundingClientRect();
	if (bottom <= 0 || top >= window.innerHeight) return 0;
	return Math.max(0, window.innerHeight - Math.max(0, top));
}

/**
 * Occupancy of a band whose own box is larger than what it shows (the chat
 * attention stack reserves a shadow gutter even when empty): measured from the
 * highest child that has a box, so an empty band clears nothing.
 */
function childrenBottomOccupancy(node: HTMLElement): number {
	let top = Infinity;
	for (const child of Array.from(node.children)) {
		const rect = child.getBoundingClientRect();
		if (rect.height > 0) top = Math.min(top, rect.top);
	}
	if (top === Infinity || top >= window.innerHeight) return 0;
	return Math.max(0, window.innerHeight - Math.max(0, top));
}

export interface ToastClearanceRegistrationOptions {
	measure?: ToastClearanceMeasurement;
}

export function createToastClearanceStore() {
	const store = writable(0);
	const registrations = new Map<HTMLElement, ToastClearanceRegistration>();

	function publish(): void {
		store.set(Math.max(0, ...[...registrations.values()].map(({ clearance }) => clearance)));
	}

	function register(
		node: HTMLElement,
		{ measure = () => viewportBottomOccupancy(node) }: ToastClearanceRegistrationOptions = {},
	): () => void {
		const update = () => {
			const registration = registrations.get(node);
			if (!registration) return;
			registration.clearance = measure();
			publish();
		};
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
		registrations.set(node, { clearance: 0, observer, update });
		observer?.observe(node);
		window.addEventListener('resize', update);
		// A phone keyboard moves the composer without resizing it or the window.
		window.visualViewport?.addEventListener('resize', update);
		window.visualViewport?.addEventListener('scroll', update);
		update();

		return () => {
			observer?.disconnect();
			window.removeEventListener('resize', update);
			window.visualViewport?.removeEventListener('resize', update);
			window.visualViewport?.removeEventListener('scroll', update);
			registrations.delete(node);
			publish();
		};
	}

	return {
		subscribe: store.subscribe,
		register,
	};
}

export const toastClearance = createToastClearanceStore();

export function toastClearanceSurface(node: HTMLElement) {
	return {
		destroy: toastClearance.register(node),
	};
}

/** For a band that holds the surfaces above the composer (the attention stack). */
export function toastClearanceBand(node: HTMLElement) {
	return {
		destroy: toastClearance.register(node, { measure: () => childrenBottomOccupancy(node) }),
	};
}
