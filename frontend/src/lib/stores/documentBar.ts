import type { Snippet } from 'svelte';
import { writable } from 'svelte/store';

/**
 * The open document's one row of controls, drawn by whichever bar holds the canvas: the app bar while the canvas is too narrow to
 * split (beside the document's name), the canvas's tab strip while it is expanded (after the tabs). The document header renders
 * these snippets in its own scope, so its controls, panel and senders stay one instance wherever the bar draws them.
 */
export type DocumentBar = {
	/** Comments and Details, at the bar's end. */
	tools: Snippet;
	/** File changed, drawn right after the document's name in the app bar; null while the file is unchanged. */
	titleEnd: Snippet | null;
};

export const documentBar = writable<DocumentBar | null>(null);

/** The ask waiting on the open document, for its Details while the ask's card is not on screen. */
export type DocumentAsk = {
	who: string;
	text: string;
	others: readonly { name: string; reason: string }[];
	/** The card is on screen at rest (not hidden under a comment sheet). */
	onCard: boolean;
};

/** Publishes `bar` until the returned function runs; it clears the store only while the store still holds this bar. */
export function publishDocumentBar(bar: DocumentBar): () => void {
	documentBar.set(bar);
	return () => documentBar.update((current) => (current === bar ? null : current));
}
