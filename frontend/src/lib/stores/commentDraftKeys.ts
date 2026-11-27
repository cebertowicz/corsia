// The keys `keptCommentDrafts` keeps a comment or reply draft under: one per place a draft is written, so a draft comes
// back wherever that place is shown again.
import type { DocViewerTarget } from '$lib/docViewerTarget';

/** One document, whichever tab or viewer shows it. */
export function documentDraftKey(target: Pick<DocViewerTarget, 'path' | 'projectId'> | null): string {
	return target ? `document:${JSON.stringify([target.projectId ?? '', target.path])}` : '';
}
/** One image: an image artifact, or one image inside a document. */
export function imageDraftKey(artifactId: string | null, image = ''): string {
	return artifactId ? `image:${JSON.stringify([artifactId, image])}` : '';
}
/** A new comment on one passage of a chat: the chat, the message and the passage's offsets, so two passages never share a draft. */
export function chatDraftKey(threadId: string, messageId: string, anchor: { from_off: number; to_off: number }): string {
	return `chat:${JSON.stringify([threadId, messageId, anchor.from_off, anchor.to_off])}`;
}
/** The chat, message and passage offsets a `chatDraftKey` names, or null for a key that is no chat comment's. */
export function chatDraftOf(key: string): { threadId: string; messageId: string; from_off: number; to_off: number } | null {
	if (!key.startsWith('chat:')) return null;
	try {
		const [threadId, messageId, from_off, to_off] = JSON.parse(key.slice('chat:'.length)) as unknown[];
		if (typeof threadId !== 'string' || typeof messageId !== 'string' || !Number.isInteger(from_off) || !Number.isInteger(to_off)) return null;
		return { threadId, messageId, from_off: from_off as number, to_off: to_off as number };
	} catch {
		return null;
	}
}
/** The scope of a reply to a chat's comment thread (`replyDraftKey`): the thread's id is a comment's own UUID, so it keys the reply wherever the card is shown. */
export const CHAT_REPLY_SCOPE = 'chat';
/** A reply to one thread, under the document or image it is written on (`documentDraftKey`, `imageDraftKey`); a thread's id alone keys it where there is neither. */
export function replyDraftKey(scope: string, commentId: string): string {
	return `reply:${JSON.stringify([scope, commentId])}`;
}
