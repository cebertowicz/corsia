import type { ChatComment, Message } from '$lib/stores/threads';
import type { AttentionDestination } from '$lib/api/attentionQueue';
import { chatReplyIndex } from '$lib/chatCommentDelivery';
// The same composition the comment cards use for a quote label: markdown marks out, whitespace flattened, then the shared snippet cut.
import { clean, snippet } from '$lib/cm/widget-cells';
import { plainQuote } from '$lib/plainQuote';

/** A comment's quote as an accessible name reads it: cut like a card's quote label and set off in curly quotes, so a screen reader hears where it starts and ends. */
export const quoteLabel = (quote: string) => `“${snippet(clean(plainQuote(quote)))}”`;

export type UnreadTarget = { quote: string; distance: number; destination: AttentionDestination };

/**
 * Where a comment is on screen: its painted highlight, or — when the quote no longer matches the
 * message and there is nothing to paint — the detached thread shown under the message. Both are
 * focusable, and the reply jumper lands on whichever exists. In Focus a message can draw as two rows
 * (its activity in a run, its text below), so every row carrying its id is searched.
 */
export function commentLanding(root: ParentNode, messageId: string, comment: Pick<ChatComment, 'id'>): HTMLElement | null {
	const rows = `[data-message-id="${CSS.escape(messageId)}"]`;
	return [...root.querySelectorAll<HTMLElement>(`${rows} [data-comment-ids][role="button"]`)]
		.find(mark => mark.dataset.commentIds?.split(',').includes(comment.id))
		?? root.querySelector<HTMLElement>(`${rows} [data-comment-uuid="${CSS.escape(comment.id)}"]`);
}

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
	let hidden: { quote: string; seq: number; destination: AttentionDestination } | null = null;
	// One pass over the thread's sends for every comment, instead of a scan of all rows per comment.
	const replies = threadId ? chatReplyIndex(messages, threadId) : null;
	for (const message of messages) {
		if (message.thread_id !== threadId || !message.chat_comments?.length || !replies) continue;
		for (const comment of message.chat_comments) {
			const unreadCount = comment.answers.filter(answer => answer.author_kind === 'agent' && answer.unread).length;
			if (!threadId || !unreadCount || replies.pending(comment).length) continue;
			count += unreadCount;
			// A jump needs the message's place in history, so a comment that cannot carry one is counted but not selected.
			if (!comment.cid || typeof message.seq !== 'number') continue;
			const destination: AttentionDestination = { pane: 'chat-replies', id: message.id, seq: message.seq, cid: comment.cid, user_message_id: comment.user_message_id };
			const element = commentLanding(root, message.id, comment);
			// Loaded but not drawn, inside a collapsed run: the reply jumper opens the run, so it is still a destination.
			if (!element) { hidden ??= { quote: comment.quote, seq: message.seq, destination }; continue; }
			const rect = element.getBoundingClientRect();
			if (rect.bottom < visibleTop) {
				const target = { quote: comment.quote, distance: visibleTop - rect.bottom, destination };
				if (!above || target.distance < above.distance) above = target;
			} else if (rect.top > visibleBottom) {
				const target = { quote: comment.quote, distance: rect.top - visibleBottom, destination };
				if (!below || target.distance < below.distance) below = target;
			}
		}
	}
	if (hidden && !above && !below) {
		// Nothing drawn either side: point at the hidden one, above or below by its place in history against the first row in view.
		const first = [...root.querySelectorAll<HTMLElement>('[data-message-seq]')].find(row => row.getBoundingClientRect().bottom > visibleTop);
		const target = { quote: hidden.quote, distance: 0, destination: hidden.destination };
		if (first && hidden.seq < Number(first.dataset.messageSeq)) above = target;
		else below = target;
	}
	return { above, below, count };
}
