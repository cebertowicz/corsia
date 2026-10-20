import { get, writable } from 'svelte/store';

interface ModelSheetRequest {
	id: number;
	focusLocalOnly: boolean;
}

/** Bumped to ask the composer to open its model sheet from elsewhere (the
 * conductor's sources panel "Choose another", #3287). */
const modelSheetRequest = writable<ModelSheetRequest>({ id: 0, focusLocalOnly: false });

export function requestModelSheet(options: { focusLocalOnly?: boolean } = {}): void {
	modelSheetRequest.update(({ id }) => ({
		id: id + 1,
		focusLocalOnly: options.focusLocalOnly === true,
	}));
}

/** Call `open` on each request made after subscribing; its focus intent travels with that request. */
export function onModelSheetRequest(open: (request: ModelSheetRequest) => void): () => void {
	let seen = get(modelSheetRequest).id;
	return modelSheetRequest.subscribe((request) => {
		if (request.id === seen) return;
		seen = request.id;
		open(request);
	});
}
