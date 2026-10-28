<script lang="ts">
	import { token } from '$lib/stores/auth';
	import { isNavAgent, messages, threads, type ChatComment } from '$lib/stores/threads';
	import { refreshChatReplyAttention } from '$lib/stores/chatReplyAttention';
	import { formatTime } from '$lib/time';
	import CommentQuote from './comments/CommentQuote.svelte';
	import CommentThread, { type CommentThreadData } from './comments/CommentThread.svelte';

	let { messageId, threadId, comments }: { messageId: string; threadId: string; comments: ChatComment[] } = $props();
	const agentName = $derived.by(() => {
		const current = $threads.find(item => item.id === threadId);
		return current && isNavAgent(current) ? current.title : 'CORSIA';
	});

	function answerId(answer: ChatComment['answers'][number]): string {
		return JSON.stringify([answer.user_message_id, answer.cid, answer.author_kind === 'agent' ? answer.assistant_message_id : null]);
	}
	function thread(comment: ChatComment): CommentThreadData {
		return {
			id: comment.cid, anchor: comment.quote, author: 'user', authorKind: 'user', text: comment.note,
			at: formatTime(comment.created_at), createdAt: comment.created_at, resolved: false,
			replies: comment.answers.map(answer => ({ id: answerId(answer), author: answer.author_kind === 'agent' ? agentName : 'user',
				authorThreadId: answer.author_kind === 'agent' ? threadId : undefined, authorKind: answer.author_kind, text: answer.text, at: formatTime(answer.created_at), createdAt: answer.created_at })),
			newReplyIds: comment.answers.filter(answer => answer.author_kind === 'agent' && answer.unread).map(answerId),
		};
	}
	async function viewed(comment: ChatComment, replyId: string): Promise<void> {
		const answer = comment.answers.find(item => answerId(item) === replyId);
		if (!answer || answer.author_kind !== 'agent') return;
		const response = await fetch(`/api/threads/${encodeURIComponent(threadId)}/messages/${encodeURIComponent(answer.user_message_id)}/comments/${encodeURIComponent(answer.cid)}/viewed`, {
			method: 'POST', headers: { Authorization: `Bearer ${$token}`, 'Content-Type': 'application/json' },
			body: JSON.stringify({ assistant_message_id: answer.assistant_message_id }),
		});
		if (!response.ok) throw new Error(`HTTP ${response.status}`);
		messages.update(current => current.map(row => row.id !== messageId ? row : {
			...row, chat_comments: (row.chat_comments ?? []).map(item => item.cid !== comment.cid || item.user_message_id !== comment.user_message_id
				? item : { ...item, answers: item.answers.map(entry => entry.author_kind === 'agent' && entry.user_message_id === answer.user_message_id && entry.cid === answer.cid && entry.created_at <= answer.created_at ? { ...entry, unread: false } : entry) }),
		}));
		await refreshChatReplyAttention(threadId);
	}
</script>

{#if comments?.length}
	<div class="chat-comment-threads">
		{#each comments as comment (`${comment.user_message_id}:${comment.cid}`)}
			<div class="chat-comment-thread" data-chat-comment={comment.cid} data-comment-uuid={comment.id} tabindex="-1">
				<CommentQuote quote={comment.quote} />
				<CommentThread comment={thread(comment)} readOnly flat viewEachUnread onViewed={(_, replyId) => viewed(comment, replyId)} />
			</div>
		{/each}
	</div>
{/if}

<style>
	.chat-comment-threads { display: flex; flex-direction: column; gap: var(--space-3); margin-top: var(--space-3); padding-left: var(--space-3); border-left: 1px solid var(--border); }
	.chat-comment-thread { display: flex; flex-direction: column; gap: var(--space-1); }
	.chat-comment-thread:global(.chat-reply-landed) { background: color-mix(in srgb, var(--accent) 24%, var(--bg-primary)); border-radius: var(--radius-sm); padding: var(--space-2); }
</style>
