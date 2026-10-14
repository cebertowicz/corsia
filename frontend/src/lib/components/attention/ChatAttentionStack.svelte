<script lang="ts">
	/**
	 * The chat canvas's one bottom-of-canvas attention slot.
	 *
	 * Chat is the only surface where several "this needs you" controls compete
	 * for the same band *and* the composer floats over that band
	 * (`InputBar.css` `.input-floater`: `position: absolute; bottom: 0;
	 * z-index: 10`). A control placed in `.chat-content`'s normal flow is
	 * therefore pushed underneath the composer and cannot be clicked at all.
	 *
	 * This component owns that slot exactly once — its position, its stacking
	 * order, its width and its height budget — and lays its occupants out in
	 * normal flow, so no occupant ever computes an offset from another
	 * occupant's height. (That alternative is not merely awkward: the lease
	 * control is `position: absolute`, so it contributes zero measurable height
	 * to anything trying to offset itself above it.)
	 *
	 * This is the sole attention mount in the composed product, so its slot
	 * owns the complete stacking and height contract.
	 */
	import type { Snippet } from 'svelte';

	let { children }: { children?: Snippet } = $props();

	let slot = $state<HTMLElement | null>(null);

	/**
	 * Publish the band's measured occupied height so the transcript can reserve
	 * exactly it. The slot is the only element that knows what is mounted, so it
	 * is the only honest place to measure; occupants never measure each other.
	 *
	 * A `ResizeObserver` covers every cause at once — an occupant mounting or
	 * unmounting, the host lease widening, and the keyboard inset moving the
	 * band — without any occupant having to announce itself.
	 */
	$effect(() => {
		const element = slot;
		if (element === null || typeof ResizeObserver === 'undefined') return;
		// The transcript is this slot's sibling, not its descendant, so the
		// measurement is published on their common `.chat-content` ancestor —
		// custom properties inherit down, never sideways.
		const host = element.closest<HTMLElement>('.chat-content') ?? element.parentElement;
		if (host === null) return;
		const publish = () => {
			/* `offsetHeight` is the border box, which now carries the gutter; the
			   transcript must reserve the band it can see, not the room its shadow
			   paints into. Read the gutter back so the number lives only in CSS. */
			const style = getComputedStyle(element);
			const gutter =
				(parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
			const height = element.firstElementChild
				? Math.max(0, element.offsetHeight - gutter)
				: 0;
			host.style.setProperty('--chat-attention-occupied-height', `${height}px`);
		};
		publish();
		const observer = new ResizeObserver(publish);
		observer.observe(element);
		return () => {
			observer.disconnect();
			host.style.removeProperty('--chat-attention-occupied-height');
		};
	});
</script>

<div class="chat-attention-stack" bind:this={slot}>
	{@render children?.()}
</div>

<style>
	.chat-attention-stack {
		/* One height budget for the whole band, published by the slot and
		   consumed by its occupants. Without it a tall occupant grows upward
		   past the header and its own controls leave the viewport (measured at
		   320px: the chip's heading and close button sat at y = -91). */
		/* InputBar consumes the keyboard inset only at its mobile breakpoint.
		   Keep this slot on the same responsive contract so focusing a desktop
		   editor cannot create an artificial gap above the composer. */
		--chat-attention-keyboard-inset: 0px;
		--chat-attention-available-height: calc(
			100dvh
			- var(--header-height, 64px)
			- var(--input-bar-height, 100px)
			- var(--chat-attention-keyboard-inset)
			- 24px
		);
		/* The clip edge below is the padding edge, so this padding *is* the room an
		   occupant's shadow has to paint in: one that fills a track sits flush with
		   it, and without room its `--glass-elevation-1` drop shadow is cut at the
		   band edge (#1322). Half the band's own 32px side inset is the largest
		   value that keeps the clip box inside the viewport at every width, which
		   is what lets `margin: 0 auto` keep centring the content band on exactly
		   the pixels it used to occupy. */
		--chat-attention-shadow-gutter: 16px;
		/* The app is `border-box` (app.css), where padding would eat the content
		   band instead of growing the clip. Here it must grow the clip only, so
		   `width` and `max-height` below keep describing the unchanged content band
		   while the border box gains the gutter. */
		box-sizing: content-box;
		padding: var(--chat-attention-shadow-gutter);
		position: absolute;
		/* Above the composer's z-index 10 — the same stacking the host-access
		   control already had to claim for itself. */
		z-index: 11;
		left: 0;
		right: 0;
		/* `bottom` positions the now gutter-larger border box, so it is pulled in by
		   one gutter to leave the painted band on its original pixels. */
		bottom: calc(
			var(--input-bar-height, 100px)
			+ var(--chat-attention-keyboard-inset)
			- var(--chat-attention-shadow-gutter)
			+ 8px
		);
		width: min(760px, calc(100% - 32px));
		margin: 0 auto;
		display: grid;
		gap: var(--space-2);
		max-height: var(--chat-attention-available-height);
		/* Without this the tracks sum to their content — measured 335 + 8 + 79 =
		   422px inside a 383px budget at 1280x577 — and `overflow: visible`
		   paints the trailing occupant 39px below the slot's own bottom anchor
		   and 12px into the composer, which the slot's z-index 11 then wins. */
		overflow: hidden;
		/* The empty band stays click-through; occupants opt back in. Scrolling
		   therefore belongs to the occupant, not to this container. */
		pointer-events: none;
	}
	/* Only a shared band needs distributing: the leading occupant absorbs the
	   shortfall and the trailing one keeps its natural height. Scoped to two
	   occupants because an unconditional two-track template always applies its
	   row-gap, which added an 8px phantom gap to the solo case (59 -> 67).

	   `:global()` inside `:has()` is required: the occupants are separate
	   components, so a bare `:nth-child(2)` compiles to a rule demanding this
	   component's scope hash on someone else's element, and silently matches
	   nothing. No occupant measures another — the slot owns its track sizing,
	   which is what a slot owner is for. */
	.chat-attention-stack:has(> :global(:nth-child(2))) {
		grid-template-rows: minmax(0, 1fr) auto;
	}
	.chat-attention-stack:has(> :global(:nth-child(2))) > :global(*:first-child) {
		/* A grid item floors at min-content height too, so the flexible track
		   only actually flexes once its occupant may shrink and scroll itself. */
		min-height: 0;
		overflow-y: auto;
	}
	.chat-attention-stack > :global(*) {
		pointer-events: auto;
		/* Grid items default to `min-width: auto`, i.e. they refuse to shrink
		   below their min-content width. A one-line bar whose label is
		   `white-space: nowrap` therefore blows past a 288px slot at 320px wide
		   (measured: a 344px dock, right edge 28px past the viewport). The slot
		   owns its track sizing, so it declares here that its occupants may
		   shrink; the occupants keep their own ellipsis rules. */
		min-width: 0;
	}
	@media (max-width: 768px) {
		.chat-attention-stack {
			/* The mobile composer moves up by this canonical inset while an editor
			   is focused. The full-width attention surfaces sit above one stable
			   three-slot utility rail: host access / live edge / Plan. */
			--chat-attention-keyboard-inset: var(--kb-toolbar-inset, 0px);
			grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
			grid-template-rows: none;
			grid-auto-rows: auto;
			/* The three compact utilities are peers on one optical baseline.
			   `end` aligned their boxes' bottoms, which puts controls of unequal
			   height on visibly different centres; they now share a height, and
			   centring keeps them honest if one ever wraps. */
			align-items: center;
		}
		.chat-attention-stack > :global(.attention-dock),
		.chat-attention-stack > :global(.sources-panel) {
			grid-column: 1 / -1;
			order: 1;
		}
		.chat-attention-stack > :global(.thread-host-access) {
			grid-column: 1;
			justify-self: start;
			order: 2;
		}
		.chat-attention-stack > :global(.thread-host-access.wide) {
			grid-column: 1 / -1;
			justify-self: stretch;
			order: 0;
		}
		.chat-attention-stack > :global(.mobile-scroll-fab) {
			grid-column: 2;
			justify-self: center;
			order: 2;
		}
		.chat-attention-stack > :global(.plan-progress-chip) {
			grid-column: 3;
			justify-self: end;
			order: 2;
		}
		/* The held-thread line describes the composer, so it sits nearest it. */
		.chat-attention-stack > :global(.plan-limit-held) {
			grid-column: 1 / -1;
			order: 3;
		}
		/* The desktop two-occupant stack contract must not impose row sizing on
		   this explicit mobile grid. */
		.chat-attention-stack:has(> :global(:nth-child(2))) {
			grid-template-rows: none;
		}
		.chat-attention-stack:has(> :global(:nth-child(2))) > :global(*:first-child) {
			overflow-y: visible;
		}
	}
	/* An out-of-flow slot no longer shortens the message list, so the list must
	   reserve the band explicitly — exactly what `HostAccessLeaseControl`
	   already does for its active chip. `:has(> *)` means an empty band reserves
	   nothing.

	   The reservation is the band's *measured* occupied height plus one gap of
	   breathing room, published as `--chat-attention-occupied-height` by the
	   slot owner. A fixed guess cannot be right for every occupancy: one 44px
	   rail, a rail under a wide host-review surface, and a rail under the
	   attention dock are materially different heights, and the previous constant
	   over-reserved the first and under-reserved the last. */
	:global(.chat-content:has(.chat-attention-stack > *) .messages) {
		padding-bottom: calc(
			var(--input-bar-height, 100px)
			+ var(--kb-toolbar-inset, 0px)
			+ var(--empty-parallax-offset, 0px)
			+ var(--chat-attention-occupied-height, 56px)
			+ var(--space-3)
		);
	}
</style>
