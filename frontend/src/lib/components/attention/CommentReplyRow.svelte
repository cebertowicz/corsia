<script lang="ts">
	import Button from '$lib/components/ui/Button.svelte';
	import IconButton from '$lib/components/ui/IconButton.svelte';
	import type { CommentReplyDetail } from '$lib/api/attentionQueue';

	let { reply, onClose = () => {}, onOpenDocument = () => {} }: {
		reply: CommentReplyDetail;
		onClose?: () => void;
		onOpenDocument?: () => void;
	} = $props();

	const remaining = $derived(reply.total_count > 1 ? `${reply.position} of ${reply.total_count}` : null);

	// Leaving for the document is not a chat click: without this the chat pane's own click handler takes the phone surface back.
	function openDocument(event: MouseEvent) {
		event.stopPropagation();
		onOpenDocument();
	}
</script>

<section class="comment-reply" aria-label="Comment reply">
	<header>
		<div class="reply-heading">
			<span class="eyebrow">Comment reply</span>
			<h3>{reply.title}</h3>
			<p class="reply-meta"><span class="reply-identity">{reply.source.thread_label} replied</span>{#if remaining}<span class="reply-position"> · {remaining}</span>{/if}</p>
		</div>
		<IconButton ariaLabel="Collapse reply" size="md" onclick={onClose}><span aria-hidden="true">×</span></IconButton>
	</header>
	<p class="excerpt">{reply.excerpt}</p>
		{#if reply.quoted_text}<p class="quoted">On “{reply.quoted_text}”</p>{/if}
	<div class="actions">
		<Button variant="primary" size="sm" onclick={openDocument}>Open in document</Button>
	</div>
</section>

<style>
	.comment-reply { background: var(--glass-regular); backdrop-filter: var(--glass-blur); border-radius: var(--radius-lg); box-shadow: var(--glass-rim), var(--glass-elevation-2); overflow: hidden; }
	header { display: flex; justify-content: space-between; gap: var(--space-3); padding: var(--space-4) var(--space-5) var(--space-2); }
	.reply-heading { min-width: 0; }
	.eyebrow { display: block; color: var(--amber-text); font-size: var(--text-caption); font-weight: var(--fw-semibold); }
	h3 { margin: var(--space-1) 0 0; color: var(--text-primary); font-size: var(--text-title3); line-height: var(--lh-tight); }
	p { margin: var(--space-1) 0 0; color: var(--text-tertiary); font-size: var(--text-caption); font-variant-numeric: tabular-nums; }
	.reply-meta { display: flex; min-width: 0; }
	.reply-identity { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.reply-position { flex: none; white-space: nowrap; }
	.excerpt { margin: 0; padding: 0 var(--space-5); color: var(--text-primary); font-size: var(--text-subhead); line-height: var(--lh-snug); }
		.quoted { overflow: hidden; margin: 0; padding: 0 var(--space-5); color: var(--text-secondary); text-overflow: ellipsis; white-space: nowrap; }
	.actions { display: flex; justify-content: flex-end; gap: var(--space-2); padding: var(--space-4) var(--space-5); }
	:global(.ico-btn:focus-visible), :global(.btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
