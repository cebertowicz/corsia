import { fetchContent, markArtifactVerified } from '$lib/api/docs';
import { on } from '$lib/ws/events';
import type { DocumentFreshness } from '$lib/docFreshness';
import { ackComment, addReply, loadComments, noteFramesBack, noteFramesLost, resolveComment } from '$lib/stores/docComments';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { commentDraftScope, discardCommentDraft } from '$lib/stores/commentDraft';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { viewerContent, type ViewerContent } from '$lib/docViewerLoading';
import { shouldReloadPresenceEdit } from '$lib/docPresence';
import { withDraftArtifactId, existingDraft } from '$lib/stores/docSaveQueue';
import { toasts } from '$lib/stores/toast';

// The viewer retains retry state; the composer needs the rejection to keep its draft.
export async function submitDocumentComment<T>(action: () => Promise<T>, onFailure?: (toastId: string) => void, failureMessage?: (error: unknown) => string): Promise<T> {
	try {
		return await action();
	} catch (error) {
		if (error instanceof DOMException && error.name === 'AbortError') throw error;
		console.error('comment failed', error);
		const toastId = toasts.show(failureMessage?.(error) ?? "Couldn't post the comment — try again", 'error');
		onFailure?.(toastId);
		throw error;
	}
}

/** Reject before a save can mutate the viewer when the active document has no artifact identity. */
export async function postDocumentComment<T>(ensureArtifactId: () => Promise<string | null>, post: (artifactId: string) => Promise<T>): Promise<T> {
	const artifactId = await ensureArtifactId();
	if (!artifactId) throw new Error('Document artifact is unavailable.');
	return post(artifactId);
}

/** Each viewer owns its failed-comment toast so one retry cannot dismiss another viewer's error. */
export function createCommentRetryToast(toastStore: { dismiss: (id: string) => void }) {
	let failureToastId: string | null = null;
	return {
		remember: (id: string) => { failureToastId = id; },
		dismissAfterSuccess: () => {
			if (!failureToastId) return;
			toastStore.dismiss(failureToastId);
			failureToastId = null;
		},
	};
}

/** Prepares a clean reload of a document another participant edited; it applies only while this tab holds no edit of its own. */
export async function prepareDocumentReload(
	target: DocViewerTarget,
	isCurrent: () => boolean,
	apply: (content: ViewerContent) => void,
): Promise<(() => void) | null> {
	const current = () => isCurrent() && shouldReloadPresenceEdit(existingDraft(target));
	if (!current()) return null;
	const fallbackArtifactId = withDraftArtifactId(target).artifactId;
	// Claim a load only at reveal: a prepared reload that is never revealed
	// must not discard a load already in flight.
	const response = await fetchContent(target.path, { projectId: target.projectId, threadId: target.threadId });
	return () => {
		if (current()) apply(viewerContent(response, fallbackArtifactId));
	};
}

export async function markFreshnessVerified(
	artifactId: string | null,
	freshness: DocumentFreshness | null,
): Promise<DocumentFreshness> {
	if (!artifactId || !freshness) throw new Error('This document cannot be marked verified.');
	await markArtifactVerified(artifactId);
	return { ...freshness, verified_at: new Date().toISOString(), age_days: 0, due_at: null, state: 'current' };
}

/** Verify an open document and hand its refreshed status back to its owning viewer. */
export async function verifyAndApplyDocumentFreshness(
	artifactId: string | null,
	freshness: DocumentFreshness | null,
	apply: (next: DocumentFreshness) => void,
): Promise<void> {
	apply(await markFreshnessVerified(artifactId, freshness));
}

export function teardownDocumentViewer(
	target: DocViewerTarget | null,
	flush: () => void,
	release: (instanceId: string) => void,
): void {
	flush();
	if (target) release(target.instanceId);
	focusedDocComment.set(null);
	discardCommentDraft(commentDraftScope(target));
}

export function subscribeToDocumentCommentChanges(
	artifactId: () => string | null,
	reload: (id: string) => void,
): () => void {
	return on('doc_comment_changed', (frame: unknown) => {
		if (!frame || typeof frame !== 'object') return;
		const id = (frame as { artifact_id?: unknown }).artifact_id;
		if (typeof id === 'string' && id === artifactId()) reload(id);
	});
}

export function reloadDocumentFeedback(
	artifactId: string | null,
	sequence: number | undefined,
	currentSequence: () => number,
	sync: { sync?: (id: string, feedback: Awaited<ReturnType<typeof loadComments>>) => void } | undefined,
): Promise<void> {
	if (!artifactId) return Promise.resolve();
	return loadComments(artifactId)
		.then((feedback) => {
			if (sequence === undefined || sequence === currentSequence()) sync?.sync?.(artifactId, feedback);
		})
		.catch((error) => console.error('comments load failed', error));
}

/** Reply, resolve and agree on the open document's threads; a reply also tells the feedback status a comment arrived. */
export function createThreadActions(artifactId: () => string | null, commentCreated: () => void) {
	return {
		async reply(id: string, text: string) {
			const artifact = artifactId();
			if (!artifact) return;
			await addReply(artifact, id, text, 'you');
			commentCreated();
		},
		resolve(id: string, resolved: boolean) { const artifact = artifactId(); if (artifact) resolveComment(artifact, id, resolved); },
		agree(id: string, agreed: boolean) { const artifact = artifactId(); if (artifact) ackComment(artifact, id, agreed); },
	};
}

/** Change and activity frames sent while the socket was down never come back: call `track` with each connection state, and `close` when the viewer closes. It tells the open document's comments of the drop, and once the connection returns reloads them. */
export function reloadAfterReconnect(artifactId: () => string | null, reload: () => void): { track: (connection: string | null) => void; close: () => void } {
	let dropped = false;
	// The document told of the drop; told it is back even if another document opened meanwhile.
	let lost: string | null = null;
	const back = () => {
		if (lost) noteFramesBack(lost);
		lost = null;
	};
	return {
		track(connection) {
			const artifact = artifactId();
			if (connection === 'disconnected' || connection === 'reconnecting') {
				dropped = true;
				if (artifact !== lost) {
					back();
					if (artifact) noteFramesLost(artifact);
					lost = artifact;
				}
			} else if (connection === 'connected' && dropped) {
				dropped = false;
				back();
				reload();
			}
		},
		close: back,
	};
}
