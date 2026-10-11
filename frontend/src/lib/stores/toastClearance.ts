import { writable } from 'svelte/store';

export type ToastClearanceEligibility = () => boolean;
export type ToastClearanceMeasurement = () => number;

export const mobileSheetMedia = '(max-width: 768px)';

export function isMobileSheetPresentation(): boolean {
	return window.matchMedia(mobileSheetMedia).matches;
}

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

export interface ToastClearanceRegistrationOptions {
	isEligible?: ToastClearanceEligibility;
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
		{
			isEligible = () => true,
			measure = () => viewportBottomOccupancy(node),
		}: ToastClearanceRegistrationOptions = {},
	): () => void {
		const update = () => {
			const registration = registrations.get(node);
			if (!registration) return;
			registration.clearance = isEligible() ? measure() : 0;
			publish();
		};
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
		registrations.set(node, { clearance: 0, observer, update });
		observer?.observe(node);
		window.addEventListener('resize', update);
		update();

		return () => {
			observer?.disconnect();
			window.removeEventListener('resize', update);
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
