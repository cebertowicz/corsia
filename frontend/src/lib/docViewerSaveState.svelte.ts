import { fromStore } from 'svelte/store';
import { docSaveDrafts, draftKey } from '$lib/stores/docSaveQueue';
import { docSaveGhost } from '$lib/stores/docSaveGhost.svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';

/**
 * The save state of the viewer's current target, sourced from the per-tab-instance save queue, never a single shared
 * "current doc" flag. `status` is the indicator the header shows: the queue's 'saved' lingers as a short ghost.
 * Call during component init.
 */
export function createDocSaveState(target: () => DocViewerTarget | null) {
	const drafts = fromStore(docSaveDrafts);
	const draft = $derived.by(() => {
		const current = target();
		return current ? drafts.current[draftKey(current)] : undefined;
	});
	const ghost = docSaveGhost(() => draft?.status, () => {
		const current = target();
		return current ? draftKey(current) : null;
	});
	return {
		get draft() { return draft; },
		get error() { return draft?.status === 'error' ? draft.error : ''; },
		get status() { return ghost.current; },
	};
}
