<script lang="ts">
	// The artifacts a message carries. An ask is a flat row under the message; a file the message names is a
	// chip in a wrapping strip (a scrolling one on a phone). Both open the artifact in the canvas; neither has
	// answer buttons, because the review bar in the opened document is the one place to answer an ask.
	import { visibleCards, type MessageCard } from '$lib/api/messageCards';
	import { paneOpen } from '$lib/stores/paneOpen';
	import AttachmentRow from './AttachmentRow.svelte';

	let {
		cards,
		more = 0,
		threadId,
		hiddenHolds,
		renderedAsks,
	}: {
		cards: MessageCard[];
		/** Cards past the cap; the last chip opens the artifact pane. */
		more?: number;
		threadId: string | null | undefined;
		/** Holds whose card renders on their blocked update row instead. */
		hiddenHolds?: ReadonlySet<string>;
		/** Holds whose ask card is on screen somewhere else in the transcript. */
		renderedAsks?: ReadonlySet<string>;
	} = $props();

	const shown = $derived(visibleCards(cards, hiddenHolds, renderedAsks));
	const asks = $derived(shown.filter((card) => card.kind === 'ask'));
	const files = $derived(shown.filter((card) => card.kind !== 'ask'));

	/** The least of the next chip that must show past the last whole chip for the scroll cue to read. */
	const PEEK = 12;
	/** A chip shows a name this long (in characters) in full; only a longer name truncates, and only down to this. */
	const NAME_CHARS = 20;
	/**
	 * On a phone the strip scrolls. Mark it while more lies past the right edge, and make sure the cue shows: the next
	 * chip starts at least PEEK px inside the edge. If it would not, chips with names over NAME_CHARS give up width
	 * first; if that is not enough, the strip's right edge moves in so the last whole chip is cut by the fade, which
	 * reads as more to scroll just as well.
	 */
	function peek(node: HTMLElement, _count: number) {
		const items = () => Array.from(node.children) as HTMLElement[];
		const mark = () => {
			node.toggleAttribute('data-more', node.scrollLeft + node.clientWidth < node.scrollWidth - 1);
			node.toggleAttribute('data-before', node.scrollLeft > 1);
		};
		const fit = () => {
			node.style.maxWidth = '';
			for (const item of items()) item.style.maxWidth = '';
			if (getComputedStyle(node).flexWrap === 'nowrap' && node.scrollWidth > node.clientWidth + 1) {
				const list = items();
				const next = list.findIndex((item) => item.offsetLeft + item.offsetWidth > node.clientWidth);
				if (next > 0) {
					let need = PEEK - (node.clientWidth - list[next].offsetLeft);
					for (let i = next - 1; i >= 0 && need > 0; i--) {
						const title = list[i].querySelector<HTMLElement>('.title');
						if (!title) continue;
						const probe = document.createElement('span');
						probe.style.cssText = `position:absolute;visibility:hidden;width:${NAME_CHARS}ch`;
						title.appendChild(probe);
						const floor = Math.min(title.scrollWidth, probe.getBoundingClientRect().width);
						probe.remove();
						const take = Math.min(title.clientWidth - floor, need);
						if (take > 0) {
							list[i].style.maxWidth = `${list[i].offsetWidth - take}px`;
							need -= take;
						}
					}
					if (node.clientWidth - list[next].offsetLeft < PEEK) {
						const last = list[next - 1];
						node.style.maxWidth = `${last.offsetLeft + last.offsetWidth - PEEK}px`;
					}
				}
			}
			mark();
		};
		// Watch the container: the strip's own width is set above, and watching it would loop.
		const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(fit) : null;
		if (node.parentElement) observer?.observe(node.parentElement);
		node.addEventListener('scroll', mark, { passive: true });
		fit();
		return {
			update: fit,
			destroy() { observer?.disconnect(); node.removeEventListener('scroll', mark); },
		};
	}
</script>

{#if shown.length > 0 || more > 0}
	<div class="cards">
		{#if asks.length > 0}
			<ul class="asks" aria-label="Artifacts">
				{#each asks as card (card.hold_id ?? card.artifact_id)}
					<li><AttachmentRow {card} {threadId} /></li>
				{/each}
			</ul>
		{/if}
		{#if files.length > 0 || more > 0}
			<ul class="chips" aria-label={asks.length > 0 ? 'Files' : 'Artifacts'} use:peek={files.length + more}>
				{#each files as card (card.hold_id ?? card.artifact_id)}
					<li><AttachmentRow {card} {threadId} variant="chip" /></li>
				{/each}
				{#if more > 0}
					<li><button type="button" class="tile more press-tint" onclick={() => paneOpen.set(true)}>+{more} more</button></li>
				{/if}
			</ul>
		{/if}
	</div>
{/if}

<style>
	.cards { display: flex; flex-direction: column; gap: var(--space-2); margin-top: var(--space-2); }
	ul { list-style: none; }
	.chips { position: relative; display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
	li { display: flex; min-width: 0; }
	.asks { max-width: 480px; }
	.asks li { display: block; }
	.more {
		display: inline-flex;
		align-items: center;
		min-height: 32px;
		padding: 0 var(--space-3);
		border: 0;
		border-radius: var(--radius-pill);
		background: transparent;
		color: var(--text-secondary);
		font: inherit;
		font-size: var(--text-subhead);
		cursor: pointer;
	}
	.more:focus-visible { outline: 2px solid var(--violet); outline-offset: -2px; }
	@media (pointer: coarse) { .more { min-height: 36px; } }
	/* A phone scrolls the strip instead of wrapping it; while more lies to the right, the next chip peeks under the
	   edge fade as the cue, and once scrolled the leading edge fades the same way. A chip's focus ring is inset, so the scroller has nothing to clip. */
	@media (max-width: 480px) {
		.chips {
			flex-wrap: nowrap;
			overflow-x: auto;
			scroll-snap-type: x proximity;
			scrollbar-width: none;
		}
		/* The action sets the attribute, which the compiler cannot see on the element. */
		.chips:global([data-more]) { mask-image: linear-gradient(to right, var(--bg-primary) calc(100% - var(--space-6)), transparent); }
		/* Once scrolled, the chip cut by the leading edge fades too, so neither edge ends in a hard cut. */
		.chips:global([data-before]) { mask-image: linear-gradient(to right, transparent, var(--bg-primary) var(--space-6)); }
		.chips:global([data-before][data-more]) { mask-image: linear-gradient(to right, transparent, var(--bg-primary) var(--space-6), var(--bg-primary) calc(100% - var(--space-6)), transparent); }
		.chips li { flex: 0 0 auto; scroll-snap-align: start; }
	}
</style>
