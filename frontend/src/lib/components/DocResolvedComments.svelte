<script lang="ts">
	import type { Snippet } from 'svelte';
	import Popover from './ui/Popover.svelte';
	import DocCommentDrawerHeading from './DocCommentDrawerHeading.svelte';
	import { drawerKeys } from '$lib/docCommentDrawer';
	/**
	 * A document's resolved comments on a desktop canvas, with nothing dimmed or covered: in the margin column's place where the
	 * column fits (the cards it holds come back when this closes), else a popover from the control that opened it. The phone's
	 * sheet is the surface's.
	 */
	let { place, count, entry, onClose, list }: {
		place: 'margin' | 'popover';
		count: number;
		/** The header control that opens and closes the list: the popover's anchor, and where focus goes back on close. */
		entry: () => HTMLElement | null;
		onClose: () => void;
		list: Snippet;
	} = $props();
	function close() { onClose(); entry()?.focus(); }
</script>

{#snippet body()}<DocCommentDrawerHeading title="Resolved" closeLabel="Close resolved comments" count={String(count)} filtered={false} onClose={close} /><div class="resolved-rows">{@render list()}</div>{/snippet}
{#if place === 'margin'}
	<section class="resolved-column" data-resolved-column aria-label="Resolved comments" use:drawerKeys={close}>{@render body()}</section>
{:else}
	<!-- The control toggles the list itself, so a press on it is not an outside press. -->
	<Popover anchor={entry()} placement="bottom-end" fill open={true} {onClose} restoreFocus={() => entry()?.focus()} keepOpenOnPointer={(target) => target instanceof Element && !!target.closest('[data-resolved-entry]')}><section class="resolved-pop" aria-label="Resolved comments">{@render body()}</section></Popover>
{/if}

<style>
	/* The margin column's room right of the text (DocViewer.css `.margin-room`), in the editor's grid cell beside the editor's scrollbar
	   (--scrollbar-size), so the list holds still while the document scrolls beside it and scrolls on its own. */
	.resolved-column { grid-area: editor; justify-self: end; position: relative; z-index: 1; display: flex; flex-direction: column; box-sizing: border-box; min-height: 0; width: calc(var(--margin-column) - var(--space-3) - var(--space-2)); margin-right: calc(var(--space-2) + var(--scrollbar-size) + var(--pane-overlay-inset, 0px)); padding-top: var(--space-1); background: var(--bg-primary); }
	.resolved-pop { display: flex; flex-direction: column; min-height: 0; }
	.resolved-rows { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; --ct-radius: var(--radius); }
</style>
