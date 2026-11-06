import type { Message } from '$lib/stores/threads';
import { pendingChatReplies } from '$lib/chatCommentDelivery';

export type UnreadTarget = { element: HTMLElement; quote: string; distance: number };

/** Find the nearest rendered unread highlight on each side of the transcript viewport. */
export function unreadTargets(root: HTMLElement, messages: Message[], threadId: string | null) {
	const bounds = root.getBoundingClientRect();
	const header = document.querySelector<HTMLElement>('.shell-header')?.getBoundingClientRect();
	const composer = root.closest('.chat-content')?.querySelector<HTMLElement>('.input-floater')?.getBoundingClientRect();
	const visibleTop = Math.max(bounds.top, header?.bottom ?? bounds.top);
	const visibleBottom = Math.min(bounds.bottom, composer?.top ?? bounds.bottom);
	let above: UnreadTarget | null = null;
	let below: UnreadTarget | null = null;
	let count = 0;
	for (const message of messages) {
		if (message.thread_id !== threadId) continue;
		const row = root.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(message.id)}"]`);
		for (const comment of message.chat_comments ?? []) {
			const unreadCount = comment.answers.filter(answer => answer.author_kind === 'agent' && answer.unread).length;
			if (!threadId || !unreadCount || pendingChatReplies(messages, threadId, comment).length) continue;
			count += unreadCount;
			if (!row) continue;
			const element = [...row.querySelectorAll<HTMLElement>('[data-comment-ids][role="button"]')]
				.find(mark => mark.dataset.commentIds?.split(',').includes(comment.id));
			if (!element) continue;
			const rect = element.getBoundingClientRect();
			if (rect.bottom < visibleTop) {
				const target = { element, quote: comment.quote, distance: visibleTop - rect.bottom };
				if (!above || target.distance < above.distance) above = target;
			} else if (rect.top > visibleBottom) {
				const target = { element, quote: comment.quote, distance: rect.top - visibleBottom };
				if (!below || target.distance < below.distance) below = target;
			}
		}
	}
	return { above, below, count };
}
