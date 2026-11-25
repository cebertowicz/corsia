import { untrack } from 'svelte';
import { writable } from 'svelte/store';

/**
 * Another surface (a plan's decision row) asks Activity to open, with one of the person's to-dos open in it. The app
 * switches to Activity; Activity opens the named item once its list has it, then clears the request. `externalId` is
 * null to open Activity on its top to-do.
 */
export const activityOpenRequest = writable<{ externalId: string | null } | null>(null);

export function openActivity(externalId: string | null = null): void {
	activityOpenRequest.set({ externalId });
}

/** Switch to Activity once per request; Activity clears it after opening the item. */
export function watchActivityOpenRequests(open: () => void): () => void {
	let last: object | null = null;
	return activityOpenRequest.subscribe((request) => {
		if (!request || request === last) return;
		last = request;
		untrack(open);
	});
}
