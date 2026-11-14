<script lang="ts">
	import { unsentSends, type UnsentSend } from '$lib/chatCommentDelivery';
	import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';
	import { retryChatComment } from './composerSend';
	import { isNavAgent, messages, threads, type ChatComment } from '$lib/stores/threads';
	import { refreshChatReplyAttention } from '$lib/stores/chatReplyAttention';
	import { chatPickKey } from '$lib/stores/docComments';
	import { formatTime } from '$lib/time';
	import CommentQuote from './comments/CommentQuote.svelte';
	import CommentThread from './comments/CommentThread.svelte';
	import type { CommentThreadData } from './comments/CommentThreadData';
	import CommentReplyList from './comments/CommentReplyList.svelte';
	import './comments/comment-link.css';
	import { apiFetch } from '$lib/api/request';

	let { messageId, threadId, comments, onReply, keepReplyVisible = false, detached = false, margin = false, picked = false, opened = false }: { messageId: string; threadId: string; comments: ChatComment[]; onReply?: (text: string) => Promise<void>; keepReplyVisible?: boolean; detached?: boolean; /** The thread is a card in the transcript's margin column, the document's resting card: its own frame, no rail or quote, since it sits level with its text, and its Reply only once picked. */ margin?: boolean; /** The margin card is the picked one. */ picked?: boolean; /** The person opened this thread from its highlight or Reply (a popover or sheet), which reads it, as picking a margin card does. */ opened?: boolean } = $props();
	const agentName = $derived.by(() => {
		const current = $threads.find(item => item.id === threadId);
		return current && isNavAgent(current) ? current.title : 'CORSIA';
	});

	/** The ended sends whose Retry is on its way, so a second press waits for the first. */
	let retrying = $state<string[]>([]);
	async function retry(send: UnsentSend): Promise<void> {
		retrying = [...retrying, send.messageId];
		try { await retryChatComment(threadId, send.messageId, send.root ? 'comment' : 'reply'); }
		finally { retrying = retrying.filter(id => id !== send.messageId); }
	}

	/** A refused reply keeps its text and shows why in its field. */
	async function postReply(comment: ChatComment, text: string): Promise<void> {
		if (onReply) await chatCommentDrafts.postReply(comment.id, text, () => onReply(text));
	}

	function answerId(answer: ChatComment['answers'][number]): string {
		return JSON.stringify([answer.user_message_id, answer.cid, answer.author_kind === 'agent' ? answer.assistant_message_id : null]);
	}
	function thread(comment: ChatComment): CommentThreadData {
		const unsent = new Set(unsentSends(comment).filter(send => !send.root).map(send => send.messageId));
		return {
			id: comment.cid, anchor: comment.quote, author: 'user', authorKind: 'user', text: comment.note,
			at: formatTime(comment.created_at), createdAt: comment.created_at, resolved: false, orphaned: detached, detached,
			// A reply that is held or whose delivery ended shows below as chat shows it, not as a delivered reply.
			replies: comment.answers.filter(answer => !unsent.has(answer.user_message_id)).map(answer => ({ id: answerId(answer), author: answer.author_kind === 'agent' ? agentName : 'user',
				authorThreadId: answer.author_kind === 'agent' ? threadId : undefined, authorKind: answer.author_kind, text: answer.text, at: formatTime(answer.created_at), createdAt: answer.created_at })),
			newReplyIds: comment.answers.filter(answer => answer.author_kind === 'agent' && answer.unread).map(answerId),
		};
	}
	async function viewed(comment: ChatComment, replyId: string): Promise<void> {
		const answer = comment.answers.find(item => answerId(item) === replyId);
		if (!answer || answer.author_kind !== 'agent') return;
		const response = await apiFetch(`/api/threads/${encodeURIComponent(threadId)}/messages/${encodeURIComponent(answer.user_message_id)}/comments/${encodeURIComponent(answer.cid)}/viewed`, {
			method: 'POST', headers: { 'Content-Type': 'application/json' },
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
				{#if !detached && !margin}<CommentQuote quote={comment.quote} />{/if}
				<CommentThread comment={thread(comment)} readOnly={!onReply} flat={!margin} inMargin={margin} focused={margin && picked} opened={margin ? picked : opened} pickKey={margin ? chatPickKey(comment.id) : undefined} scrollOnFocus={!margin} hideReply={margin && !picked} hideOrphan={!detached} detachedNote="This text can’t be highlighted in the message." {keepReplyVisible} bind:replyText={() => chatCommentDrafts.get(comment.id), (text) => chatCommentDrafts.set(comment.id, text)} replyPending={!!chatCommentDrafts.reply(comment.id)?.posting} replyError={chatCommentDrafts.reply(comment.id)?.error ?? ''} chatSurface onViewed={(_, replyId) => viewed(comment, replyId)} onReply={async (_, text) => { for (const answer of comment.answers.filter(item => item.author_kind === 'agent' && item.unread)) void viewed(comment, answerId(answer)).catch(error => console.warn('Could not record the viewed reply', error)); await postReply(comment, text); }}>
				{#snippet failure()}
				{#each unsentSends(comment) as send (send.messageId)}
					<div class="failed-reply" class:own={send.root} role="status">
						<!-- The comment's own text is the thread's body above; only a reply, kept out of the delivered list, shows its text here. -->
						{#if !send.root}<CommentReplyList replies={[{ id: send.messageId, author: 'user', authorKind: 'user', text: send.note, at: formatTime(send.createdAt) }]} />{/if}
						<!-- Chat's label and red mark for a message in this state (UserMessageBubble). -->
						{#if send.delivery.failed}<span class="delivery-failed-mark" title={send.delivery.title} aria-label="Needs your attention">!</span>{/if}
						<span class="delivery-status" class:terminal={send.delivery.failed} title={send.delivery.title}>{send.delivery.label}</span>
						{#if send.retry}<button class="ct-link" title={send.retry.title} disabled={retrying.includes(send.messageId)} onclick={() => retry(send)}>{send.retry.label}</button>{/if}
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
	.failed-reply > .delivery-status { color: var(--text-secondary); font-size: var(--text-footnote); font-style: italic; margin-left: var(--space-1); }
	.failed-reply > .delivery-status.terminal { color: var(--red); }
	.delivery-failed-mark { display: inline-flex; align-items: center; justify-content: center; width: var(--space-4); height: var(--space-4); margin-left: var(--space-3); border-radius: var(--radius-circle, 50%); background: var(--red); color: var(--accent-on-fill); font-size: var(--text-caption); font-weight: 700; }
	/* Retry is the one way back for a failed comment, so on touch it is a full target. */
	@media (pointer: coarse) { .failed-reply > .ct-link { min-width: var(--touch-target); min-height: var(--touch-target); padding-inline: var(--space-2); } }
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
