import { get, writable } from 'svelte/store';

/** Bumped to ask the composer to open its model sheet from elsewhere (the
 * conductor's sources panel "Choose another", #3287). */
const modelSheetRequest = writable(0);

/** The Local only badge asks for the sheet with its switch row focused. */
const localOnlyFocusRequest = writable(0);
const FOCUS_REQUEST_LIFETIME_MS = 2000;
let focusRequestedAt = 0;

export function requestModelSheet(options: { focusLocalOnly?: boolean } = {}): void {
	if (options.focusLocalOnly) {
		focusRequestedAt = Date.now();
		localOnlyFocusRequest.update((count) => count + 1);
	}
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

/** Call `focus` for a focus request made while this row is mounted, or just before the sheet mounted it. */
export function onLocalOnlyFocusRequest(focus: () => void): () => void {
	let seen = get(localOnlyFocusRequest);
	if (Date.now() - focusRequestedAt < FOCUS_REQUEST_LIFETIME_MS) focus();
	focusRequestedAt = 0;
	return localOnlyFocusRequest.subscribe((request) => {
		if (request === seen) return;
		seen = request;
		focusRequestedAt = 0;
		focus();
	});
}
