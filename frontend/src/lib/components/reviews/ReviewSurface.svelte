<script lang="ts">
	import { tick } from 'svelte';
	import Popover from '$lib/components/ui/Popover.svelte';
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import ReviewPicker from './ReviewPicker.svelte';
	import ReviewList from './ReviewList.svelte';
	import { Button } from '$lib/components/ui';
	import type { DocumentReviewState } from '$lib/docReviewState.svelte';
	let { model, id, anchor, narrow, canRequest, onSent, onHide, restoreFocus }: {
		model: DocumentReviewState; id: string; anchor: HTMLElement | null; narrow: boolean; canRequest: boolean;
		onSent: () => void; onHide: () => void; restoreFocus: () => void;
	} = $props();
	let content = $state<HTMLElement>();
	function close() { onHide(); restoreFocus(); }
	$effect(() => { void tick().then(() => content?.querySelector<HTMLElement>('[data-review-autofocus]')?.focus()); });
</script>

{#snippet body()}
	<section {id} data-review-surface aria-label="Document reviews" bind:this={content}>
		{#if canRequest}<ReviewPicker {model} {id} {onSent} />{/if}
		{#if model.loadState === 'error' && !canRequest}<p class="hint" role="status">Couldn't load review requests. <Button size="sm" variant="ghost" onclick={() => model.load()}>Retry</Button></p>{/if}
		<ReviewList {model} />
	</section>
{/snippet}
{#if narrow}
	<AdaptiveDrawer fill ariaLabel="Document reviews" heading="Request review" doneLabel="Close" onClose={close} initialFocusSelector="[data-review-autofocus]">{@render body()}</AdaptiveDrawer>
{:else}
	<Popover fill {anchor} open={true} onClose={onHide} {restoreFocus} placement="bottom">{@render body()}</Popover>
{/if}

<style>
	/* Popover and drawer both `fill`: the section takes the room they bound and scrolls inside it. */
	section { display: flex; flex-direction: column; gap: 8px; flex: 1; min-width: 0; min-height: 0; overflow: auto; }
	.hint { margin: 0; color: var(--text-secondary); font-family: var(--font-sans); font-size: var(--text-footnote); }
	:global([data-adaptive-drawer]:has([data-review-surface]) .drawer-done) { min-height: 44px; }
</style>
