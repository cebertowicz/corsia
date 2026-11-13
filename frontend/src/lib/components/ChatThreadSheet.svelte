<script lang="ts">
	import type { ComponentProps } from 'svelte';
	import type { ChatComment } from '$lib/stores/threads';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover from './ui/Popover.svelte';
	import ChatCommentThreads from './ChatCommentThreads.svelte';

	/**
	 * The sheet (phone) or popover (wide) a chat highlight's comment opens in: one thread, or the list of every thread on the pressed text
	 * (`listed`). A thread opened from that list says Back in the header's leading slot. The dialog is named by what its header says:
	 * "Comment" for one, the count for the list.
	 */
	let { messageId, threadId, comments, listed, fromList, closing, narrow, anchor, bounds, panel = $bindable(null), onReply, onBack, onClose, onClosed }: {
		messageId: string; threadId: string; comments: ChatComment[]; listed: boolean; fromList: boolean; closing: boolean; narrow: boolean;
		anchor: ComponentProps<typeof Popover>['anchor']; bounds: ComponentProps<typeof Popover>['bounds']; panel?: HTMLElement | null;
		onReply: (note: string) => Promise<void>; onBack: () => void; onClose: () => void; onClosed: () => void;
	} = $props();

	const name = $derived(listed ? `${comments.length} comments` : 'Comment');
	const title = name;
</script>

{#snippet backButton()}<button type="button" class="thread-back" aria-label="Back to the list of comments" onclick={onBack}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6" /></svg><span>Back</span></button>{/snippet}
{#snippet body(keep: boolean)}
	<ChatCommentThreads {messageId} {threadId} {comments} onReply={listed ? undefined : onReply} keepReplyVisible={!listed && keep} />
{/snippet}
{#if narrow}
	<AdaptiveDrawer bind:panel ariaLabel={name} heading={title} leading={fromList ? backButton : undefined} doneLabel="Close" opaque followKeyboard open={!closing} {onClose} {onClosed}>
		{@render body(true)}
	</AdaptiveDrawer>
{:else}
	<Popover {anchor} {bounds} placement="bottom" clearAnchor open={!closing} surfaceRole={null} {onClose} {onClosed}>
		<div role="dialog" aria-label={name}>
			{#if listed}<h2 class="thread-list-title">{name}</h2>{/if}
			{#if fromList}{@render backButton()}{/if}
			{@render body(false)}
		</div>
	</Popover>
{/if}

<style>
	/* The way back to the list: the Back of the other drill-in screens (chevron and the word), a touch-sized target. */
	.thread-back { display: inline-flex; align-items: center; gap: var(--space-1); min-height: var(--touch-target); min-width: var(--touch-target); padding: 0 var(--space-2); border: 0; border-radius: var(--radius-xs); background: transparent; color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); font-weight: var(--fw-medium); cursor: pointer; }
	.thread-back:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.thread-list-title { margin: 0; padding: var(--space-1) var(--space-3) 0; color: var(--text-secondary); font-size: var(--text-subhead); font-weight: var(--fw-semibold); }
</style>
