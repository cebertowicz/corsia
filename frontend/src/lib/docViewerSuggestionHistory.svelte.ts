import { SuggestionRefused, transitionSuggestionHistory } from '$lib/api/commentSuggestions';
import type { SuggestionHistoryEntry } from '$lib/cm/suggestionHistory';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { loadComments } from '$lib/stores/docComments';
import { flushDraft, seedDraft } from '$lib/stores/docSaveQueue';
import { textSha256 } from '$lib/stores/documentWire';
import { toasts } from '$lib/stores/toast';

const CHANGE_FAILED = 'Couldn’t change the suggestion. Reload and try again.';

/** Coordinate a history transition with the current viewer and its save queue. */
export function createViewerSuggestionHistory(source: {
	target: () => DocViewerTarget | null;
	artifact: () => string | null;
	unavailable: () => boolean;
	isLive: () => boolean;
	accept: (text: string, eventId: string, ids: string[]) => boolean;
	setText: (text: string) => void;
	setBusy: (busy: boolean) => void;
	reload: () => void;
}) {
	let busy = $state(false);
	async function change(entry: SuggestionHistoryEntry, before: string, after: string): Promise<boolean> {
		const doc = source.target(), artifact = source.artifact();
		if (!doc || !artifact || source.unavailable()) return false;
		busy = true;
		source.setBusy(true);
		try {
			let result: Awaited<ReturnType<typeof transitionSuggestionHistory>>;
			try {
				if (!await flushDraft(doc) || source.target()?.instanceId !== doc.instanceId) return false;
				result = await transitionSuggestionHistory(artifact, entry.eventId, [...entry.ids], entry.direction, await textSha256(before), after);
				if (typeof result.text !== 'string' || typeof result.revision !== 'string') throw new Error('Missing suggestion history result');
			} catch (error) {
				// Only the server's own written refusal reaches the person; any other error is code or network text.
				if (source.target()?.instanceId === doc.instanceId) {
					toasts.show(error instanceof SuggestionRefused ? error.message : CHANGE_FAILED, 'error');
					source.reload();
				}
				return false;
			}
			// The server has applied the change. A failure from here on is this side's follow-up, never an error to the person.
			try {
				seedDraft(doc, result.text, result.revision);
				await loadComments(artifact);
			} catch (followUpError) {
				console.error('[suggestion history] follow-up after the server applied it failed', followUpError);
				if (source.target()?.instanceId === doc.instanceId) source.reload();
			}
			return source.target()?.instanceId === doc.instanceId;
		} finally {
			busy = false;
			source.setBusy(false);
		}
	}
	function showAccepted(text: string, receipt?: { eventId: string; ids: string[] }) {
		if (source.isLive()) return;
		if (!receipt || !source.accept(text, receipt.eventId, receipt.ids)) source.setText(text);
	}
	return { get busy() { return busy; }, change, showAccepted };
}
