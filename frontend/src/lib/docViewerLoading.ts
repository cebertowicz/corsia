import { fetchContent, fetchRevision, type DocContent } from '$lib/api/docs';
import type { DocumentFreshness } from '$lib/docFreshness';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { DocOpenError, docOpenRefusal, liveUnavailable, SNAPSHOT_REFUSAL_MESSAGE, type DocOpenRefusal, type LiveUnavailable } from '$lib/docOpenRefusal';
import { withDraftArtifactId, enrichDraftTarget, existingDraft, flushDraft, type DocSaveDraft } from '$lib/stores/docSaveQueue';
import { openLiveDocument } from '$lib/stores/liveDocuments';
import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
import type { TabLoadSignal } from '$lib/artifactTabs';

export interface ViewerContent {
	text: string;
	artifactId: string | null;
	writable: boolean;
	freshness: DocumentFreshness | null;
	revision: string | null;
	live: LiveDocumentSession | null;
	/** Why a live open answered non-live, when the server said. */
	liveUnavailable?: LiveUnavailable | null;
}
/** The viewer's form of an HTTP content response, which never carries a live session. */
export function viewerContent(response: DocContent, fallbackArtifactId: string | null): ViewerContent {
	return { text: response.text, artifactId: response.artifact?.id ?? fallbackArtifactId, writable: response.writable !== false,
		freshness: response.freshness, revision: response.revision ?? null, live: null, liveUnavailable: liveUnavailable(response) };
}
interface LoadOptions {
	target: DocViewerTarget;
	isCurrent: () => boolean;
	onReady: (content: ViewerContent) => void;
	onSnapshot: (text: string) => void;
	onUnavailable: () => void;
}

/** Product Experience approved this body for a refused tab showing the file snapshot. */
export { SNAPSHOT_REFUSAL_MESSAGE } from '$lib/docOpenRefusal';
/** A disconnected tab keeps the snapshot disclosure truthful while saying it will reopen. */
export const NOT_CONNECTED_SNAPSHOT_REFUSAL_MESSAGE = "This tab shows the last saved version of the file. Changes from this browser that weren't saved yet aren't shown here. It reopens when the connection returns.";

/** What a refused tab's editor shows; a refusal never reads the device copy. */
export type RefusedView = 'draft' | 'snapshot' | 'none';

/**
 * The body a refusal may carry for what its editor shows. The server's body
 * may say text in this tab is kept, which holds only while the editor shows
 * this tab's draft. Showing the file snapshot instead, the body says exactly
 * that and claims nothing kept; title and code, and so Copy and Retry, stay.
 */
export function refusalForView(refusal: DocOpenRefusal, view: RefusedView): DocOpenRefusal {
	if (view !== 'snapshot') return refusal;
	return { ...refusal, message: refusal.code === 'not_connected' ? NOT_CONNECTED_SNAPSHOT_REFUSAL_MESSAGE : SNAPSHOT_REFUSAL_MESSAGE };
}

/** How a tab's document shows the moment it mounts, before the loader answers. */
export interface DraftOpen {
	/** The text its draft holds, shown at once; null when the tab waits for the loader. */
	text: string | null;
	/** Unsaved local text wins over server content, but not over write authority: it stays read-only until the loader re-checks write access. */
	unsaved: boolean;
	/** Saved text, read-only while the loader confirms it against the live document. */
	confirming: boolean;
	signal: Omit<TabLoadSignal, 'instanceId'>;
}

/** A tab whose draft holds its text shows that text at once; one without waits for the loader. */
export function openFromDraft(draft: DocSaveDraft | undefined, artifactId: string | null): DraftOpen {
	if (!draft) return { text: null, unsaved: false, confirming: false, signal: { status: 'loading' } };
	const unsaved = draft.editText !== draft.savedText;
	// Saved text claims no write access until the loader answers, so its tab shows no read-only mark meanwhile.
	const signal: DraftOpen['signal'] = { status: 'ready', artifactId, shown: true, ...(unsaved ? { writable: false } : {}) };
	return { text: draft.editText, unsaved, confirming: !unsaved, signal };
}

export async function loadViewerContent(options: LoadOptions): Promise<void> {
	let snapshotShown = false;
	try {
		await load({ ...options, onSnapshot: (text) => { snapshotShown = true; options.onSnapshot(text); } });
	} catch (error) {
		const refusal = docOpenRefusal(error);
		if (!refusal || !options.isCurrent()) throw error;
		// The viewer puts an existing draft back in the editor on refusal.
		const view: RefusedView = existingDraft(options.target) ? 'draft' : snapshotShown ? 'snapshot' : 'none';
		// Nothing of this document reached the editor: never leave the previous one there.
		if (view === 'none') options.onSnapshot('');
		throw new DocOpenError(refusalForView(refusal, view));
	}
}

async function load(options: LoadOptions): Promise<void> {
	const { target, isCurrent } = options;
	const draft = existingDraft(target);
	if (draft && draft.editText !== draft.savedText) {
		// Strings in a legacy draft carry no Yjs identities. Save before live
		// admission; a document_live refusal leaves this draft available to copy.
		const revision = await fetchRevision(target.path, target.projectId, draft.target.artifactId);
		if (!isCurrent()) return;
		if (!revision.exists) { options.onUnavailable(); return; }
		if (revision.artifact_id) enrichDraftTarget(target, revision.artifact_id);
		if (!revision.writable || !await flushDraft(target)) {
			if (isCurrent()) options.onReady({ text: existingDraft(target)?.editText ?? draft.editText,
				artifactId: revision.artifact_id ?? draft.target.artifactId, writable: revision.writable,
				freshness: revision.freshness, revision: null, live: null });
			return;
		}
	}
	if (!isCurrent()) return;
	const effective = withDraftArtifactId(target);
	const response = await fetchContent(effective.path, {
		projectId: effective.projectId, threadId: effective.threadId, liveOpen: true,
	});
	if (!isCurrent()) return;
	options.onSnapshot(existingDraft(target)?.editText ?? response.text);
	const live = await openLiveDocument(effective);
	if (!isCurrent()) return;
	if (live && typeof live !== 'string') {
		options.onReady({ text: live.text.toString(), artifactId: live.artifactId, writable: live.writable,
			freshness: response.freshness, revision: null, live });
		// A device copy replayed on reopen changes the document without an edit, so no autosave ever asks for the
		// file write: request it only when that replay left the file behind the live text.
		if (live.writable && live.durability.unwritten && live.text.toString() !== response.text) void flushDraft(effective);
		return;
	}
	// Non-live: the HTTP or the socket answer may say why; either way the legacy route edits it.
	const content = viewerContent(response, effective.artifactId);
	options.onReady({ ...content, liveUnavailable: content.liveUnavailable ?? live });
}
