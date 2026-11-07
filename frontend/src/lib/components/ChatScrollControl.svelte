<script lang="ts">
	import { tick } from 'svelte';
	import { messages } from '$lib/stores/threads';
	import { chatReplyAttention } from '$lib/stores/chatReplyAttention';
	import type { AttentionDestination } from '$lib/api/attentionQueue';
	import { emitProductEvent } from '$lib/product_events';
	import { quoteLabel, unreadTargets, type UnreadTarget } from './chatUnreadTargets';

	let { threadId, userIsNearBottom, onScrollToBottom, onJumpToReply, scroller }: {
		threadId: string | null;
		userIsNearBottom: boolean;
		onScrollToBottom: () => void;
		onJumpToReply: (destination: AttentionDestination) => void;
		scroller?: HTMLElement;
	} = $props();

	let target = $state<UnreadTarget | null>(null);
	let direction = $state<'above' | 'below'>('below');
	let loadedCount = $state(0);
	function update() {
		const result = scroller ? unreadTargets(scroller, $messages, threadId) : null;
		const above = result?.above ?? null, below = result?.below ?? null;
		direction = above && (!below || above.distance <= below.distance) ? 'above' : 'below';
		target = direction === 'above' ? above : below;
		loadedCount = result?.count ?? 0;
	}
	$effect(() => {
		const root = scroller;
		void $messages; void threadId;
		if (!root) { target = null; loadedCount = 0; return; }
		let frame = 0;
		const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(update); };
		root.addEventListener('scroll', schedule, { passive: true });
		const observer = new MutationObserver(schedule);
		observer.observe(root, { childList: true, subtree: true });
		void tick().then(schedule);
		return () => { root.removeEventListener('scroll', schedule); observer.disconnect(); cancelAnimationFrame(frame); };
	});
	const attention = $derived(threadId && $chatReplyAttention?.threadId === threadId
		? $chatReplyAttention.items.find(item => item.detail.destination.pane === 'chat-replies') : undefined);
	const unreadCount = $derived(attention?.detail.unread_count ?? loadedCount);
	const unloaded = $derived(!target && !!attention && unreadCount > loadedCount
		&& !$messages.some(message => message.thread_id === threadId && message.id === attention.detail.destination.id));
	function jump() {
		// One landing path for both cases: the reply jumper loads whatever pages the answer is still waiting in,
		// scrolls, focuses and emphasises the comment, and opens it when the comment carries a highlight.
		const destination = target?.destination ?? (unloaded ? attention?.detail.destination : undefined);
		if (destination) {
			// Counts how often the control replaces the removed reply chip as the way to an unread reply.
			emitProductEvent('chat', 'reply_jumped', { in_older_messages: !target });
			return onJumpToReply(destination);
		}
		onScrollToBottom();
	}
	const visible = $derived(!!target || unloaded || (!userIsNearBottom && !!threadId));
	// The accessible name carries the same count the button shows, so the two can never disagree (COPY-STANDARD C5).
	const countText = $derived(`${unreadCount} new ${unreadCount === 1 ? 'reply' : 'replies'}`);
	const label = $derived(target ? `Go to the next new reply ${direction}: ${quoteLabel(target.quote)}. ${countText}.`
		: unloaded ? `Go to the next new reply in older messages. ${countText}.`
			: 'Scroll to the bottom');
</script>

{#if visible}
	<button class="rail-control chat-scroll-control press-tint" class:unread={!!target || unloaded} type="button" onclick={jump} aria-label={label} title={label}>
		{#if target || unloaded}<span aria-hidden="true">{unloaded || direction === 'above' ? '↑' : '↓'}</span><span>{unreadCount} new</span>
		{:else}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12l7 7 7-7" /></svg>{/if}
	</button>
{/if}

<style>
	.chat-scroll-control {
		display: inline-flex; width: var(--touch-target); height: var(--touch-target); padding: 0;
		align-items: center; justify-content: center; gap: var(--space-1);
		border: 1px solid color-mix(in srgb, var(--border) 72%, transparent); border-radius: var(--radius-pill);
		background: var(--glass-regular-thick); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-elevation-1), var(--glass-rim); color: var(--text-secondary); cursor: pointer;
	}
	.chat-scroll-control.unread { width: auto; padding: 0 var(--space-3); }
	@media (min-width: 769px) {
		.chat-scroll-control { width: calc(var(--space-7) + var(--space-1)); height: calc(var(--space-7) + var(--space-1)); }
		.chat-scroll-control.unread { width: auto; }
	}
	.chat-scroll-control svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
	.chat-scroll-control:hover { color: var(--text-primary); }
	.chat-scroll-control:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
</style>
