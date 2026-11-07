import type { DocComment } from '$lib/stores/docComments';
import { loadComments } from '$lib/stores/docComments';
import { authorsWithOpenSuggestions as groupOpenSuggestions } from '$lib/docCommentDrawer';
import { decideSuggestion, SuggestionRefused, type SuggestionDecision } from '$lib/api/commentSuggestions';
import { flushDraft, seedDraft } from '$lib/stores/docSaveQueue';
import { recordRefusal, clearRefusal, clearRefusals } from '$lib/stores/suggestionRefusals';
import type { DocViewerTarget } from '$lib/docViewerTarget';

/** What the comments surface hands the suggestion decisions: the shown comments and document, and the callbacks that keep the editor in step. */
export type SuggestionDecisionInputs = {
	comments: () => DocComment[];
	artifactId: () => string | null;
	target: () => DocViewerTarget | null;
	open: () => boolean;
	onSuggestionBusy?: (busy: boolean, id?: string) => void;
	onSuggestionAccepted?: (text: string) => void;
	onDocumentChanged?: () => void;
};

/** Accept, reject and accept-all for one comments surface, with what one opening of it shows from them. Call it while a component initialises. */
export function createSuggestionDecisions(inputs: SuggestionDecisionInputs) {
		const authorsWithOpenSuggestions = $derived(groupOpenSuggestions(inputs.comments()));
		// Whose accept-all is running. It disables every accept-all on purpose, across documents: one request at a time.
		let acceptAllBusy = $state<string | null>(null);
		// What one opening of the surface shows from its decisions: each card's failure (the surface makes the request, and a
		// card that moves groups remounts and would lose its own), the accept-all error, the skipped list and whose accept-all
		// it shows as running. A new document or opening replaces it; a decision writes only through `settle`, to the outcome
		// it started with, so nothing of it lands on what replaced it.
		type Outcome = { errors: Record<string, string>; acceptAllError: string; skipped: { id: string; reason: string }[]; acceptAllShownBusy: string | null };
		const fresh = (): Outcome => ({ errors: {}, acceptAllError: '', skipped: [], acceptAllShownBusy: null });
		let outcome = $state<Outcome>(fresh());
		function settle(started: Outcome, change: (current: Outcome) => void) { if (started === outcome) change(outcome); }
		// The reset ends this document's refusals too: reopening the comments offers Accept again. It follows the surface opening and
		// closing only, never a move from one card or view to another inside it.
		const open = $derived(inputs.open());
		$effect(() => {
			const artifact = inputs.artifactId(); void open;
			outcome = fresh();
			return () => { if (artifact) clearRefusals(artifact); };
		});
		const reason = (error: unknown) => (error instanceof Error ? error.message : String(error));
		async function submitSuggestionDecision(id: string, decision: 'accept' | 'reject', allFromAuthor = false) {
			// The document this decision is for. The props may name another one by the time it settles.
			const started = outcome, doc = inputs.target(), artifact = inputs.artifactId();
			inputs.onSuggestionBusy?.(true, decision === 'accept' ? id : undefined);
			try {
				if (decision === 'accept') {
					if (!doc || !await flushDraft(doc)) throw new Error("Couldn't save the document before accepting this suggestion");
				}
				const result: SuggestionDecision = allFromAuthor
					? await decideSuggestion(id, decision, true)
					: await decideSuggestion(id, decision);
				settle(started, (current) => { current.skipped = result.skipped ?? []; });
				if (decision === 'accept' && result.text !== undefined && result.revision !== undefined && doc) {
					// The accepted text goes on screen only while that document's tab is still the one shown; closing the surface does not change that.
					const current = inputs.target(), shown = current?.instanceId === doc.instanceId ? current : null;
					seedDraft(shown ?? doc, result.text, result.revision);
					if (shown) inputs.onSuggestionAccepted?.(result.text);
				}
				if (artifact) await loadComments(artifact);
			} catch (error) {
				// Bound to the document, like the accepted text. A decision made elsewhere comes back with a comments load; an
				// edit applied but not recorded is in the saved file, so the shown document is read again with its comments.
				const kind = error instanceof SuggestionRefused ? error.kind : null;
				if (kind === 'unrecorded' && doc && inputs.target()?.instanceId === doc.instanceId) inputs.onDocumentChanged?.();
				else if ((kind === 'decided' || kind === 'unrecorded') && artifact) void loadComments(artifact).catch(() => undefined);
				throw error;
			} finally {
				inputs.onSuggestionBusy?.(false);
			}
		}
		async function decideOne(id: string, decision: 'accept' | 'reject') {
			const started = outcome, artifact = inputs.artifactId();
			started.errors = { ...started.errors, [id]: '' };
			try {
				await submitSuggestionDecision(id, decision);
				if (artifact) clearRefusal(artifact, id);
			} catch (error) {
				// The saved file no longer has the quote: the card stays orphaned with this reason until it is decided or the surface resets.
				const stale = decision === 'accept' && error instanceof SuggestionRefused && error.kind === 'stale';
				settle(started, (current) => {
					if (stale && artifact) recordRefusal(artifact, id, reason(error));
					else current.errors = { ...current.errors, [id]: reason(error) };
				});
			}
		}
		async function acceptAllFrom(author: string, id: string) {
			if (acceptAllBusy !== null) return;
			const started = outcome;
			acceptAllBusy = author; started.acceptAllShownBusy = author; started.acceptAllError = '';
			try {
				await submitSuggestionDecision(id, 'accept', true);
			} catch (error) {
				settle(started, (current) => { current.acceptAllError = reason(error); });
			} finally {
				acceptAllBusy = null;
				settle(started, (current) => { current.acceptAllShownBusy = null; });
			}
		}

	return {
		get authorsWithOpenSuggestions() { return authorsWithOpenSuggestions; },
		get acceptAllBusy() { return acceptAllBusy; },
		get outcome() { return outcome; },
		decideOne,
		acceptAllFrom,
	};
}
