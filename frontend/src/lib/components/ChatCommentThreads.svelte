<script lang="ts">
	import { token } from '$lib/stores/auth';
	import { chatReplyIndex } from '$lib/chatCommentDelivery';
	import { requestDeliveryAction } from '$lib/deliveryActions';
	import { isNavAgent, messages, threads, type ChatComment } from '$lib/stores/threads';
	import { refreshChatReplyAttention } from '$lib/stores/chatReplyAttention';
	import { formatTime } from '$lib/time';
	import CommentQuote from './comments/CommentQuote.svelte';
	import CommentThread from './comments/CommentThread.svelte';
	import type { CommentThreadData } from './comments/CommentThreadData';
	import CommentReplyList from './comments/CommentReplyList.svelte';
	import { chatReplyDrafts } from './chatReplyDrafts.svelte';
	import './comments/comment-link.css';

	let { messageId, threadId, comments, onReply, keepReplyVisible = false, detached = false, margin = false }: { messageId: string; threadId: string; comments: ChatComment[]; onReply?: (text: string) => void; keepReplyVisible?: boolean; detached?: boolean; /** The thread is a card in the transcript's margin column: no rail, since the card is its frame. */ margin?: boolean } = $props();
	// Built once per change of the loaded rows, then read per comment: the pending and failed sends of this thread.
	const replies = $derived(chatReplyIndex($messages, threadId));
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
			at: formatTime(comment.created_at), createdAt: comment.created_at, resolved: false, orphaned: detached, detached,
			replies: comment.answers.filter(answer => answer.author_kind !== 'user' || !replies.failed(comment).some(failed => failed.id === answer.user_message_id)).map(answer => ({ id: answerId(answer), author: answer.author_kind === 'agent' ? agentName : 'user',
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
	<div class="chat-comment-threads" class:margin>
		{#each comments as comment (`${comment.user_message_id}:${comment.cid}`)}
			<div class="chat-comment-thread" data-chat-comment={comment.cid} data-comment-uuid={comment.id} tabindex="-1">
				{#if !detached}<CommentQuote quote={comment.quote} />{/if}
				<CommentThread comment={thread(comment)} readOnly={!onReply} flat hideOrphan={!detached} detachedNote="This text can’t be highlighted in the message." {keepReplyVisible} bind:replyText={() => chatReplyDrafts.get(comment.id), (text) => chatReplyDrafts.set(comment.id, text)} chatSurface onViewed={(_, replyId) => viewed(comment, replyId)} onReply={async (_, text) => { onReply?.(text); for (const answer of comment.answers.filter(item => item.author_kind === 'agent' && item.unread)) void viewed(comment, answerId(answer)).catch(error => console.warn('Could not record the viewed reply', error)); }}>
				{#snippet failure()}
				{#each replies.pending(comment) as pending (pending.id)}
					<div class="pending-reply" role="status">
						{#each pending.metadata.reply_context.comments.filter((reply: { root_user_message_id?: string; root_cid?: string }) => reply.root_user_message_id === comment.user_message_id && reply.root_cid === comment.cid) as reply}
							<CommentReplyList replies={[{ id: pending.id, author: 'user', authorKind: 'user', text: reply.note, at: formatTime(pending.created_at) }]} />
						{/each}
						<span>Sending…</span>
					</div>
				{/each}
				{#each replies.failed(comment) as failed (failed.id)}
					<div class="failed-reply" role="status">
						{#each failed.metadata.reply_context.comments.filter((reply: { root_user_message_id?: string; root_cid?: string }) => reply.root_user_message_id === comment.user_message_id && reply.root_cid === comment.cid) as reply}
							<CommentReplyList replies={[{ id: failed.id, author: 'user', authorKind: 'user', text: reply.note, at: formatTime(failed.created_at) }]} />
						{/each}
						<span>Not delivered</span>
						<button class="ct-link" onclick={() => requestDeliveryAction('retry', { threadId, messageId: failed.id, authToken: $token ?? '', failureText: 'Couldn’t retry this reply. Try again.' })}>Retry</button>
					</div>
				{/each}
				{/snippet}
				</CommentThread>
				{#if !onReply}<button class="ct-link reply-link" data-reply-comment={comment.id} aria-label={`Reply to comment on “${comment.quote}”`} onclick={(event) => document.dispatchEvent(new CustomEvent('chat-comment-reply', { detail: { threadId, messageId, commentId: comment.id, target: event.currentTarget } }))}><span class="reply-label">Reply</span></button>{/if}
			</div>
		{/each}
	</div>
{/if}

<style>
	.pending-reply > span { color: var(--text-secondary); font-size: var(--text-footnote); margin-left: var(--space-3); }
	.failed-reply > span { color: var(--red); font-size: var(--text-footnote); margin-left: var(--space-3); }
	.chat-comment-threads { display: flex; flex-direction: column; gap: var(--space-3); margin-top: var(--space-3); padding-left: var(--space-3); border-left: 1px solid var(--border); }
	/* In a margin card the card is the frame, so the rail under the message is not drawn. */
	.chat-comment-threads.margin { margin: 0; padding: 0; border: 0; }
	.chat-comment-thread { display: flex; flex-direction: column; gap: var(--space-1); }
	.reply-link { align-self: flex-start; min-width: var(--touch-target); text-align: left; padding: 0; margin: calc(-1 * var(--space-1)) 0 calc(-1 * var(--space-1)) calc(var(--space-3) - var(--chip-inline-padding)); }
	.reply-label { display: inline-block; padding: var(--space-1) var(--chip-inline-padding); border-radius: var(--radius-sm); }
	.reply-link:focus-visible { outline: none; }
	.reply-link:focus-visible .reply-label { outline: 2px solid var(--accent); outline-offset: -2px; }
	@media (pointer: coarse) { .reply-link { min-height: var(--touch-target); padding: var(--space-3) 0; margin-top: calc(-1 * var(--space-4)); margin-bottom: calc(-1 * var(--space-4)); } }
	.chat-comment-thread:global(.chat-reply-landed) { background: color-mix(in srgb, var(--accent) 24%, var(--bg-primary)); border-radius: var(--radius-sm); padding: var(--space-2); }
</style>
