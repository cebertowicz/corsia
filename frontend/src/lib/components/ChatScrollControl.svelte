<script lang="ts">
	import { tick } from 'svelte';
	import { messages } from '$lib/stores/threads';
	import { chatReplyAttention } from '$lib/stores/chatReplyAttention';
	import type { AttentionDestination } from '$lib/api/attentionQueue';
	import { unreadTargets, type UnreadTarget } from './chatUnreadTargets';

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
		if (unloaded && attention) return onJumpToReply(attention.detail.destination);
		if (!target) return onScrollToBottom();
		const element = target.element;
		element.scrollIntoView({ block: 'center', behavior: 'smooth' });
		element.focus({ preventScroll: true });
		element.click();
	}
	const visible = $derived(!!target || unloaded || (!userIsNearBottom && !!threadId));
	const label = $derived(target ? `${unreadCount} new ${unreadCount === 1 ? 'reply' : 'replies'}, next ${direction}: '${target.quote}'` : unloaded ? 'New reply' : 'Scroll to bottom');
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
