<script lang="ts">
	import type { Snippet } from 'svelte';
	import { scrollCue } from '$lib/actions/scrollCue';

	/** The body of an open suggestion's card. */
	let { children }: { children: Snippet } = $props();
</script>

<div class="ct-scroll" use:scrollCue>{@render children()}</div>

<style>
	/* In a popover the card is bounded by the popover: its body is the one scrolling area, and the Accept and Reject row sits below it, so nothing is drawn under the row. Elsewhere the wrapper takes no box. */
	.ct-scroll { display: contents; }
	:global([data-popover].fill > .list:has(> .ct.scrolling)) { min-height: 0; }
	:global([data-popover] .ct.scrolling) { min-height: 0; }
	:global([data-popover] .ct.scrolling) > .ct-scroll { display: flex; flex-direction: column; gap: inherit; min-height: 0; overflow-y: auto; scrollbar-width: thin; --cue-top: 0px; --cue-bottom: 0px; mask-image: linear-gradient(to bottom, transparent, black var(--cue-top), black calc(100% - var(--cue-bottom)), transparent); }
	:global([data-popover] .ct.scrolling) > .ct-scroll:global([data-above='true']) { --cue-top: var(--space-2); }
	:global([data-popover] .ct.scrolling) > .ct-scroll:global([data-more='true']) { --cue-bottom: var(--space-2); }
	:global([data-popover] .ct.scrolling) > .ct-scroll > :global(*) { flex-shrink: 0; }
</style>
