import { untrack } from 'svelte';
import { restoreVersion, type DocVersion } from '$lib/docHistory';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { existingDraft, flushDraft } from '$lib/stores/docSaveQueue';
import { contentWidth, widenForHistory } from '$lib/imageViewerPane';
import { emitProductEvent } from '$lib/product_events';

/** Whether History is offered and open for the viewer's tab, and the one call that asks the server to restore a version. */
export function createViewerHistory(source: {
	key: () => string;
	target: () => DocViewerTarget | null;
	closeComments: () => void;
	root: () => HTMLElement | undefined;
}) {
	let open = $state(false);
	// The server says on open whether this file's versions are recorded; History is fetched only once opened.
	let available = $state(false);
	// A restore is pending, so the document's save line reads "Saving…".
	let restoring = $state(false);

	$effect(() => {
		if (!source.key()) return;
		return () => { open = false; available = false; restoring = false; };
	});

	$effect(() => {
		if (!open) return;
		const root = source.root();
		const canvas = root?.closest<HTMLElement>('.leaf-body') ?? root;
		if (!canvas) return;
		const { width, inset } = untrack(() => contentWidth(canvas));
		return widenForHistory(width, inset);
	});

	function toggle(): void {
		source.closeComments();
		if (open) { open = false; return; }
		if (!source.target()) return;
		open = true;
		emitProductEvent('document', 'history_opened');
	}
	/** Saves typing already made, then asks the server to restore against the text that save left on disk. */
	async function restore(version: DocVersion): Promise<void> {
		const document = source.target();
		if (!document) throw new Error('Document closed');
		if (!await flushDraft(document)) throw new Error('Pending typing was not saved');
		const base = existingDraft(document)?.savedRevision;
		if (!base) throw new Error('Document revision unavailable');
		await restoreVersion(document, version.id, base);
	}
	return {
		get open() { return open; },
		get available() { return available; },
		get restoring() { return restoring; },
		setAvailable: (value: boolean) => { available = value; },
		setRestoring: (busy: boolean) => { restoring = busy; },
		close: () => { open = false; }, toggle, restore,
	};
}
