<script lang="ts">
	// The chip a message carries when its tool call opened the shared browser in front of the person. It sits in the
	// file chip strip and looks like a file chip, but it is not an artifact: it shows the thread's one live browser
	// view again, whatever page that view is on now.
	import { BROWSER_CHIP_NAME, browserChipLabel, type BrowserChip } from '$lib/browserChip';
	import { emitProductEvent } from '$lib/product_events';
	import { openBrowserPane } from '$lib/stores/artifactWorkspace';
	import ArtifactGlyph from './ArtifactGlyph.svelte';

	let { chip, threadId }: { chip: BrowserChip; threadId: string } = $props();

	function open(event: MouseEvent): void {
		// The chat column marks chat the active surface on any click inside it; bubbling there would
		// flip the pane just opened back to chat, which on a narrow canvas hides it.
		event.stopPropagation();
		emitProductEvent('browser_chip', 'pane_open');
		openBrowserPane(threadId);
	}
</script>

<!-- The visible host is the accessible name, so voice control can say it and each chip is told apart; the title is its description. -->
<button type="button" class="tile chip press-tint" title={BROWSER_CHIP_NAME} onclick={open}>
	<ArtifactGlyph icon="globe" />
	<span class="text"><span class="head"><span class="title">{browserChipLabel(chip)}</span></span></span>
</button>

<style>
	/* The file chip's geometry and tokens (AttachmentRow's chip), so the two read as one strip. */
	.tile {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		min-width: 0;
		max-width: 13.75rem;
		min-height: 2rem;
		padding: 0 var(--space-3);
		border: 0;
		border-radius: var(--radius-pill);
		background: var(--bg-tertiary);
		color: var(--text-primary);
		font: inherit;
		text-align: left;
		cursor: pointer;
		transition: background var(--dur-tight) var(--ease-snap);
	}
	@media (hover: hover) {
		.tile:hover { background: var(--bg-hover); }
	}
	/* Inset: the strip scrolls on a phone and clips an outward ring. */
	.tile:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	@media (pointer: coarse) {
		.tile { min-height: 2.25rem; }
	}
	.text { display: flex; flex-direction: column; flex: 0 1 auto; min-width: 0; }
	.head { display: flex; align-items: baseline; gap: var(--space-2); min-width: 0; }
	.title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
		line-height: var(--lh-tight);
	}
	@media (prefers-reduced-motion: reduce) {
		.tile { transition: none; }
	}
</style>
