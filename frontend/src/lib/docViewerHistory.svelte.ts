import { tick, untrack } from 'svelte';
import { restoreVersion, type DocVersion } from '$lib/docHistory';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { existingDraft, flushDraft } from '$lib/stores/docSaveQueue';
import { contentWidth, widenForHistory } from '$lib/imageViewerPane';
import { emitProductEvent } from '$lib/product_events';
import { RequestRefused } from '$lib/api/docs';
import { textSha256 } from '$lib/stores/documentWire';

/** Whether History is offered and open for the viewer's tab, and the one call that asks the server to restore a version. */
export function createViewerHistory(source: {
	key: () => string;
	target: () => DocViewerTarget | null;
	closeComments: () => void;
	/** The margin column is up beside the text now. */
	columnShown: () => boolean;
	/** Brings the column back, when it fits and has comments to show. */
	showColumn: () => void;
	root: () => HTMLElement | undefined;
	/** An open live document's current text, the base a restore is checked against; null for a saved-file tab. */
	liveText?: () => string | null;
}) {
	let open = $state(false);
	// The server says on open whether this file's versions are recorded; History is fetched only once opened.
	let available = $state(false);
	// A restore is pending, so the document's save line reads "Saving…"; the History surface sets it and reads it here.
	let restoring = $state(false);
	// The column was up when History opened (opening it closes the comments), so closing History puts it back.
	let columnWas = false;

	$effect(() => {
		if (!source.key()) return;
		return () => { open = false; available = false; restoring = false; columnWas = false; };
	});

	$effect(() => {
		if (!open) return;
		const root = source.root();
		const canvas = root?.closest<HTMLElement>('.leaf-body') ?? root;
		if (!canvas) return;
		const { width, inset } = untrack(() => contentWidth(canvas));
		return widenForHistory(width, inset);
	});

	function close(): void {
		if (!open) return;
		open = false;
		if (!columnWas) return;
		columnWas = false;
		// The canvas is only back to its own width once the widened History layout has gone.
		void tick().then(source.showColumn);
	}
	function toggle(): void {
		const showing = source.columnShown();
		source.closeComments();
		if (open) { close(); return; }
		if (!source.target()) return;
		columnWas = showing;
		open = true;
		emitProductEvent('document', 'history_opened');
	}
	/** Saves typing already made, then asks the server to restore against the text that save left on disk. A refusal before sending carries its cause as `kind`. */
	async function restore(version: DocVersion): Promise<void> {
		const document = source.target();
		if (!document) throw new RequestRefused('Document closed', 'not_loaded');
		const flushed = await flushDraft(document).catch(() => false);
		if (!flushed) throw new RequestRefused('Pending typing was not saved', 'unsaved_typing');
		const live = source.liveText?.() ?? null;
		const base = live !== null ? await textSha256(live) : existingDraft(document)?.savedRevision;
		if (!base) throw new RequestRefused('Document revision unavailable', 'not_loaded');
		await restoreVersion(document, version.id, base);
	}
	return {
		get open() { return open; },
		get available() { return available; },
		get restoring() { return restoring; },
		setAvailable: (value: boolean) => { available = value; },
		setRestoring: (busy: boolean) => { restoring = busy; },
		close, toggle, restore,
	};
}
