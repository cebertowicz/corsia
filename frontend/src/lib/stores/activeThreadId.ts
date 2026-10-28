import { writable, get } from 'svelte/store';

// A draft choice belongs to the current chat surface and is discarded when it changes.
export const draftAccountId = writable<string | null>(null);
const activeThreadIdStore = writable<string | null>(null);
export const activeThreadId = {
	subscribe: activeThreadIdStore.subscribe,
	set(id: string | null) {
		activeThreadIdStore.set(id);
		draftAccountId.set(null);
	},
	update(fn: (id: string | null) => string | null) { this.set(fn(get(activeThreadIdStore))); },
};
