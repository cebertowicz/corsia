import { get, writable } from 'svelte/store';

export interface DeliveryConfirmation {
	id: string;
	artifactId: string;
	message: string;
}

const DELIVERY_CONFIRMATION_MS = 3_000;

/**
 * Held-comment delivery is confirmed by the document canvas that owns the
 * delivered artifact, rather than by the app-wide toast anchor. The canvas
 * host keeps the notice in the content area at every responsive layout.
 */
function createDeliveryConfirmationStore() {
	const store = writable<DeliveryConfirmation | null>(null);
	let timer: ReturnType<typeof setTimeout> | null = null;

	function dismiss(id?: string) {
		const current = get(store);
		if (id && current?.id !== id) return;
		if (timer) clearTimeout(timer);
		timer = null;
		store.set(null);
	}

	function show(artifactId: string, sent: number) {
		const confirmation = {
			id: Math.random().toString(36).slice(2),
			artifactId,
			message: `${sent} comment${sent === 1 ? '' : 's'} sent`,
		};
		dismiss();
		store.set(confirmation);
		timer = setTimeout(() => dismiss(confirmation.id), DELIVERY_CONFIRMATION_MS);
		return confirmation.id;
	}

	return { subscribe: store.subscribe, show, dismiss };
}

export const deliveryConfirmation = createDeliveryConfirmationStore();
