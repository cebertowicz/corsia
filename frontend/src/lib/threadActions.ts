// Reply, resolve and agree on the threads of an open document or image. A reply the person pressed Send on goes on its own
// if its post does not (`unsentComments`), as a new comment does.
import { submitDocumentComment } from '$lib/docViewerOperations';
import { ackComment, addReply, EarlierVersionPosted, resolveComment } from '$lib/stores/docComments';
import { keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import { unsentOutcome } from '$lib/unsentCommentOutcome';
import { createMentionSelection, type MentionOccurrence } from '$lib/components/comments/mentionSelection';

/** A reply also tells the feedback status a comment arrived. */
export function createThreadActions(artifactId: () => string | null, commentCreated: () => void, threadId: () => string | null = () => null) {
	return {
		/**
		 * Post a reply kept under `draftKey` (`replyDraftKey`): every press posts under the draft's one id, a failure keeps
		 * the text with the reason the card shows, and text typed while it posted stays (`keptCommentDrafts.posted`). From the
		 * press, a reply on a document's or an image's thread goes on its own if this post does not, until it posts, is refused
		 * or is edited; a failure that passes is not told meanwhile, and one that is told waits for the person.
		 */
		async reply(id: string, text: string, taggedAgentIds: string[] = [], mentions: MentionOccurrence[] = [], draftKey = '') {
			const artifact = artifactId();
			if (!artifact) return;
			keptCommentDrafts.clearError(draftKey);
			const kept = keptCommentDrafts.get(draftKey);
			const press = keptCommentDrafts.claim(draftKey, kept?.body ?? text, kept?.mentionSelection ?? createMentionSelection(text), undefined, { artifactId: artifact, threadId: threadId() });
			let failure = '', toastId: string | null = null, waits = false;
			try {
				await submitDocumentComment(
					async () => {
						try {
							return await addReply(artifact, id, text, 'you', taggedAgentIds, threadId(), mentions, press.clientId, press.again);
						} catch (error) {
							waits = unsentOutcome(error) === 'retry' && unsentComments.sending(draftKey);
							throw error;
						}
					},
					(shown, message) => { if (!waits) { toastId = shown; failure = message; } },
					undefined, () => waits || keptCommentDrafts.onScreen(draftKey),
				);
			} catch (error) {
				const now = keptCommentDrafts.get(draftKey);
				if (now && failure) keptCommentDrafts.fail(draftKey, now.body, now.mentionSelection, failure, error instanceof EarlierVersionPosted);
				if (failure) {
					keptCommentDrafts.unsend(draftKey);
					keptCommentDrafts.toast(draftKey, toastId);
				}
				throw error;
			}
			// Posted: the draft goes with any toast that told an earlier press failed.
			keptCommentDrafts.posted(draftKey, text, taggedAgentIds);
			commentCreated();
		},
		resolve(id: string, resolved: boolean, wait = false): Promise<boolean> { const artifact = artifactId(); return artifact ? resolveComment(artifact, id, resolved, wait) : Promise.resolve(false); },
		agree(id: string, agreed: boolean) { const artifact = artifactId(); if (artifact) ackComment(artifact, id, agreed); },
	};
}
