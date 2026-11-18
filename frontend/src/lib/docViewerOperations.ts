import { fetchContent, markArtifactVerified, RequestRefused } from '$lib/api/docs';
import { on } from '$lib/ws/events';
import type { DocumentFreshness } from '$lib/docFreshness';
import { ackComment, addReply, loadComments, resolveComment } from '$lib/stores/docComments';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { commentDraftScope, dropCommentDraft } from '$lib/stores/commentDraft';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { viewerContent, type ViewerContent } from '$lib/docViewerLoading';
import { shouldReloadPresenceEdit } from '$lib/docPresence';
import { withDraftArtifactId, existingDraft } from '$lib/stores/docSaveQueue';
import { toasts } from '$lib/stores/toast';

/** One message per known reason the server refuses a comment's tags, picked by the refusal's `kind`. */
const TAG_REFUSALS: Record<string, string> = {
	no_viewing_thread: 'Open this document from a chat to tag agents. Your comment is still here.',
	tag_unavailable: 'An agent you tagged can’t be tagged from this chat. Remove its tag and post again. Your comment is still here.',
};

/** The server's reason a comment write is refused because its document no longer takes comments, as the 409's `kind`. */
export const NOT_COMMENTABLE_KIND = 'not_commentable';

export const isNotCommentable = (error: unknown): boolean => error instanceof RequestRefused && error.kind === NOT_COMMENTABLE_KIND;

/** That refusal's own sentence, as the server gave it (the 409 body's `detail`); it says the draft is kept, so no "try again" goes with it. */
export function notCommentableMessage(error: unknown): string | undefined {
	if (!isNotCommentable(error)) return undefined;
	let detail: unknown;
	try { detail = JSON.parse((error as Error).message.replace(/^\d{3}\s*/, ''))?.detail; } catch { /* A body that is not JSON has no sentence to show. */ }
	return typeof detail === 'string' && detail ? detail : undefined;
}

function tagRefusal(error: unknown): string | undefined {
	return error instanceof RequestRefused && error.kind ? TAG_REFUSALS[error.kind] : undefined;
}

// The viewer retains retry state; the composer needs the rejection to keep its draft, and `message` to say why in it.
export async function submitDocumentComment<T>(action: () => Promise<T>, onFailure?: (toastId: string, message: string) => void, failureMessage?: (error: unknown) => string | undefined): Promise<T> {
	try {
		return await action();
	} catch (error) {
		if (error instanceof DOMException && error.name === 'AbortError') throw error;
		console.error('comment failed', error);
		// A refusal with a named reason has the same cause on every surface, so its message comes before the surface's own.
		// The not-commentable sentence is the server's, shown as given: it is never the surface's "try again".
		const message = notCommentableMessage(error) ?? tagRefusal(error) ?? failureMessage?.(error) ?? "Couldn't post the comment — try again";
		const toastId = toasts.show(message, 'error');
		onFailure?.(toastId, message);
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
	dropCommentDraft(commentDraftScope(target));
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
export function createThreadActions(artifactId: () => string | null, commentCreated: () => void, threadId: () => string | null = () => null) {
	return {
		async reply(id: string, text: string, taggedAgentIds: string[] = [], mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[] = []) {
			const artifact = artifactId();
			if (!artifact) return;
			await submitDocumentComment(() => addReply(artifact, id, text, 'you', taggedAgentIds, threadId(), mentions));
			commentCreated();
		},
		resolve(id: string, resolved: boolean, wait = false): Promise<boolean> { const artifact = artifactId(); return artifact ? resolveComment(artifact, id, resolved, wait) : Promise.resolve(false); },
		agree(id: string, agreed: boolean) { const artifact = artifactId(); if (artifact) ackComment(artifact, id, agreed); },
	};
}
