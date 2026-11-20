import { transitionSuggestionHistory } from '$lib/api/commentSuggestions';
import type { SuggestionHistoryEntry } from '$lib/cm/suggestionHistory';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { loadComments } from '$lib/stores/docComments';
import { flushDraft, seedDraft } from '$lib/stores/docSaveQueue';
import { toasts } from '$lib/stores/toast';

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
			if (!await flushDraft(doc) || source.target()?.instanceId !== doc.instanceId) return false;
			const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(before));
			const sha = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
			const result = await transitionSuggestionHistory(artifact, entry.eventId, [...entry.ids], entry.direction, sha, after);
			if (typeof result.text !== 'string' || typeof result.revision !== 'string') throw new Error('Missing suggestion history result');
			seedDraft(doc, result.text, result.revision);
			await loadComments(artifact);
			return source.target()?.instanceId === doc.instanceId;
		} catch (error) {
			if (source.target()?.instanceId === doc.instanceId) {
				toasts.show(error instanceof Error ? error.message : 'Couldn’t change the suggestion. Reload and try again.', 'error');
				source.reload();
			}
			return false;
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
