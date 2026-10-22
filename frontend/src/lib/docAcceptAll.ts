import { acceptAllSuggestions, SuggestionRefused } from '$lib/api/commentSuggestions';
import { flushDraft, seedDraft } from '$lib/stores/docSaveQueue';
import { loadComments, type DocComment } from '$lib/stores/docComments';
import type { DocViewerTarget } from '$lib/docViewerTarget';

/** Open suggestions the review bar's accept can apply: the ones the comments pane offers to accept, not orphaned ones. */
export function openSuggestionCount(comments: DocComment[]): number {
	return comments.filter((comment) => !comment.resolved && !comment.orphaned && comment.replacement !== null && comment.replacement !== undefined).length;
}

/**
 * Accept every open suggestion on the shown document in one write, then put the accepted text on screen like a single accept does.
 * Throws when the document could not be saved first or the server refused, so the caller sends no verdict.
 */
export async function acceptAllOpen(
	target: DocViewerTarget | null, artifactId: string,
	hooks: { onText: (text: string) => void; onDocumentChanged: () => void },
): Promise<void> {
	if (!target || !await flushDraft(target)) throw new Error("Couldn't save the document before accepting the suggestions");
	try {
		const result = await acceptAllSuggestions(artifactId);
		// Nothing to accept comes back with a null text and revision: the screen stays as it is.
		if (typeof result.text === 'string' && typeof result.revision === 'string') {
			seedDraft(target, result.text, result.revision);
			hooks.onText(result.text);
		}
	} catch (error) {
		// An edit applied but not recorded is in the saved file: the shown document is read again with its comments.
		if (error instanceof SuggestionRefused && error.kind === 'unrecorded') hooks.onDocumentChanged();
		throw error;
	}
	await loadComments(artifactId);
}
