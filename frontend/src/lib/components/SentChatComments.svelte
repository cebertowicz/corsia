<script lang="ts">
	import type { ReplyComment } from '$lib/stores/replyContextCapture';
	import { isLongMessageText } from '$lib/messageCollapse';
	import CommentQuote from './comments/CommentQuote.svelte';
	let { comments, threadId }: { comments: ReplyComment[]; threadId: string | null } = $props();
	let expanded = $state<Set<string>>(new Set());
	function toggle(id: string) {
		const next = new Set(expanded);
		if (next.has(id)) next.delete(id); else next.add(id);
		expanded = next;
	}
	function goTo(seq: number) {
		if (!threadId) return;
		document.querySelector<HTMLElement>(`.messages [data-message-thread-id="${CSS.escape(threadId)}"][data-message-seq="${seq}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
	}
</script>

<div class="sent-comments">
	{#each comments as comment (comment.id)}
		{@const isExpanded = expanded.has(comment.id)}
		{@const isLong = isLongMessageText(comment.quote) || isLongMessageText(comment.note)}
		<div class="sent-comment" data-comment-id={comment.id}>
			<button class="sent-comment-source" onclick={() => goTo(comment.seq)}>
				<CommentQuote quote={comment.quote} bubble lines={isExpanded ? null : 1} />
				{#if comment.note}<span class="sent-note" class:clamped={isLong && !isExpanded}>{comment.note}</span>{/if}
			</button>
			{#if isLong}<button class="long-message-toggle sent-comment-toggle" aria-expanded={isExpanded} onclick={() => toggle(comment.id)}>{isExpanded ? 'Show less' : 'Show more'}</button>{/if}
		</div>
	{/each}
</div>

<style>
	.sent-comments { display: flex; flex-direction: column; gap: var(--space-2); margin-bottom: var(--space-2); }
	.sent-comment { display: flex; flex-direction: column; gap: var(--space-1); }
	.sent-comment-source { all: unset; box-sizing: border-box; display: flex; flex-direction: column; gap: var(--space-1); min-height: var(--list-row-min-height); justify-content: center; cursor: pointer; border-radius: var(--radius-sm); color: var(--accent-on-fill); }
	.sent-comment-source:focus-visible, .sent-comment-toggle:focus-visible { outline: 2px solid currentColor; outline-offset: 2px; }
	.sent-note { color: var(--accent-on-fill); font-size: var(--text-callout); line-height: 1.5; padding-left: calc(var(--space-2) + 2px); overflow-wrap: anywhere; white-space: pre-wrap; }
	.sent-note.clamped { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 4; line-clamp: 4; }
	.long-message-toggle.sent-comment-toggle { align-self: flex-start; color: var(--accent-on-fill); opacity: 1; }
	@media (max-width: 768px) { .sent-comment-source { min-height: var(--touch-target); } }
</style>
