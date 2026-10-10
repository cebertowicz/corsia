import { get, writable } from 'svelte/store';

/** Bumped to ask the composer to open its model sheet from elsewhere (the
 * conductor's sources panel "Choose another", #3287). */
const modelSheetRequest = writable(0);

export function requestModelSheet(): void {
	modelSheetRequest.update((count) => count + 1);
}

/** Call `open` on each request made after subscribing; returns the unsubscribe. */
export function onModelSheetRequest(open: () => void): () => void {
	let seen = get(modelSheetRequest);
	return modelSheetRequest.subscribe((request) => {
		if (request === seen) return;
		seen = request;
		open();
	});
}
