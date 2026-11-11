import { watchChatCommentThreads } from '$lib/chatCommentRefresh';
import { quoteLabel } from './chatUnreadTargets';

/**
 * What the chat's live region announces: a notice per new reply on a comment in the chat on screen. The live region
 * announces what is added to it, so this keeps the three newest, keyed so each reply adds a node, and clears them when
 * the chat changes.
 */
export function createReplyAnnouncements(pageSize: number, threadId: () => string | null) {
	let items = $state<{ id: number; text: string }[]>([]);
	$effect(() => {
		void threadId();
		items = [];
		return watchChatCommentThreads(pageSize, quote => {
			items = [...items.slice(-2), { id: (items.at(-1)?.id ?? 0) + 1, text: `New reply on ${quoteLabel(quote)}` }];
		});
	});
	return { get items() { return items; } };
}
