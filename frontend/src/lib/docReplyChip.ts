import { hasNewReplies, type DocComment } from '$lib/stores/docComments';

/** New replies across a document's threads: the chat chip's unit, so both show the same number. */
export function unreadReplyCount(threads: DocComment[]): number {
	return threads.reduce((total, thread) => total + (thread.newReplyIds?.length ?? 0), 0);
}

/**
 * The thread a tap on the chip goes to: the next one with a new reply after the one it last went to, in document order,
 * coming back to the first after the last. Going by place in the document, not by the unread list, keeps a tap moving
 * forward when the thread it left has since cleared.
 */
export function nextUnreadThread(inDocumentOrder: DocComment[], lastId: string | null): DocComment | null {
	const from = lastId ? inDocumentOrder.findIndex((thread) => thread.id === lastId) : -1;
	return inDocumentOrder.slice(from + 1).find(hasNewReplies) ?? inDocumentOrder.find(hasNewReplies) ?? null;
}
