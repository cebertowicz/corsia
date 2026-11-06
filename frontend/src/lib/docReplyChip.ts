import { hasUnread, type DocComment } from '$lib/stores/docComments';

/** Unread comments across a document's threads: the chat chip's unit, so both show the same number. */
export function unreadCommentCount(threads: DocComment[]): number {
	return threads.reduce((total, thread) => total + (thread.newReplyIds?.length ?? 0) + (thread.newComment ? 1 : 0), 0);
}

/**
 * The thread a tap on the chip goes to: the next one with a new reply after the one it last went to, in document order,
 * coming back to the first after the last. Going by place in the document, not by the unread list, keeps a tap moving
 * forward when the thread it left has since cleared.
 */
export function nextUnreadThread(inDocumentOrder: DocComment[], lastId: string | null, excludeId?: string): DocComment | null {
	const from = lastId ? inDocumentOrder.findIndex((thread) => thread.id === lastId) : -1;
	const waiting = (thread: DocComment) => thread.id !== excludeId && hasUnread(thread);
	return inDocumentOrder.slice(from + 1).find(waiting) ?? inDocumentOrder.find(waiting) ?? null;
}
