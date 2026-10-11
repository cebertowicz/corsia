<script module lang="ts">
	/** What a popover is anchored to: a rect, an element, or a reading of where the anchor is drawn now. */
	export type Anchor = DOMRect | HTMLElement | (() => DOMRect | null) | null;
	/** Fired on `window` when an anchor's text may have moved without a scroll or resize (an edit, or lines drawn after a scroll). */
	export const ANCHOR_MOVED = 'popover-anchor-moved';
</script>

<script lang="ts">
	import { tick } from 'svelte';
	let { anchor, open, onClose, restoreFocus, placement = 'right', fill = false, bounds = null, avoidRect, children }: {
		anchor: Anchor;
		open: boolean;
		onClose: () => void;
		restoreFocus?: () => void;
		placement?: 'right' | 'bottom';
		/** Make the content a column bounded by the popover, so one child can shrink and scroll while the rest stay in view. */
		fill?: boolean;
		/** Keep the surface inside this element (the canvas it belongs to) instead of anywhere in the window. */
		bounds?: HTMLElement | null;
		/** A live rectangle the card should clear vertically when it cannot fit beside its anchor. */
		avoidRect?: () => DOMRect | null;
		children: import('svelte').Snippet;
	} = $props();
	let surface = $state<HTMLElement>();
	let opener = $state<HTMLElement | null>(null);
	let style = $state('left: 12px; top: 12px;');
	/** A live anchor whose text is not drawn, or is scrolled out of the area, hides its surface; it returns when the text does. */
	let hidden = $state(false);
	function rectFor(value: Anchor) { return typeof value === 'function' ? value() : value instanceof HTMLElement ? value.getBoundingClientRect() : value; }
	/** The area the surface may occupy: the viewport, or the part of `bounds` inside it. */
	function region() {
		const edge = bounds?.getBoundingClientRect();
		return {
			left: Math.max(0, edge?.left ?? 0), top: Math.max(0, edge?.top ?? 0),
			right: Math.min(window.innerWidth, edge?.right ?? window.innerWidth), bottom: Math.min(window.innerHeight, edge?.bottom ?? window.innerHeight),
		};
	}
	function position() {
		const rect = rectFor(anchor);
		const live = typeof anchor === 'function';
		if (!surface) return;
		if (!rect) { if (live) hidden = true; return; }
		const area = region();
		if (rect.bottom < area.top || rect.top > area.bottom || rect.right < area.left || rect.left > area.right) {
			if (live) hidden = true; else dismiss();
			return;
		}
		hidden = false;
		// Measure the natural size: a width cap from an earlier placement must not shrink what a roomier one may use.
		const capped = surface.style.maxWidth;
		surface.style.maxWidth = '';
		const box = surface.getBoundingClientRect();
		surface.style.maxWidth = capped;
		const gap = 8;
		const width = Math.min(box.width, area.right - area.left - 2 * gap);
		const height = Math.min(box.height, area.bottom - area.top - 2 * gap);
		const avoided = avoidRect?.();
		let left = rect.left;
		let top = rect.bottom + gap;
		if (placement === 'bottom') {
			if (left + width > area.right - gap) left = rect.right - width;
		} else if (rect.right + gap + width <= area.right - gap) {
			// Beside the anchor, never over it.
			left = rect.right + gap; top = rect.top;
		} else if (rect.left - gap - width >= area.left + gap) {
			left = rect.left - width - gap; top = rect.top;
		} else if (avoided && avoided.bottom + gap + height <= area.bottom - gap) {
			// For an image region, use empty canvas below the image before covering another tag.
			top = avoided.bottom + gap;
		} else if (avoided && avoided.top - gap - height >= area.top + gap) {
			// When the lower canvas is full, use the empty canvas above the image instead.
			top = avoided.top - height - gap;
		} else if (top + height > area.bottom - gap && rect.top - gap - height >= area.top + gap) {
			// No room beside or outside the avoided rectangle: above the anchor. Otherwise it stays below it, clamped into the canvas.
			top = rect.top - height - gap;
		}
		top = Math.max(area.top + gap, Math.min(top, area.bottom - height - gap));
		left = Math.max(area.left + gap, Math.min(left, area.right - width - gap));
		style = `left: ${left}px; top: ${top}px; max-width: ${width}px;`;
	}
	$effect(() => {
		if (!open) return;
		window.addEventListener(ANCHOR_MOVED, position);
		return () => window.removeEventListener(ANCHOR_MOVED, position);
	});
	$effect(() => {
		if (!open) return;
		hidden = false;
		opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		void tick().then(() => { position(); surface?.querySelector<HTMLElement>('button, input, textarea, select, [tabindex]:not([tabindex="-1"])')?.focus(); });
	});
	// A new anchor (a card that stops following its text) is placed at once, not at the next scroll or edit.
	$effect(() => {
		void anchor;
		if (open) void tick().then(position);
	});
	function dismiss() { onClose(); if (restoreFocus) restoreFocus(); else opener?.focus(); }
	function onPointer(event: PointerEvent) { if (open && surface && event.target instanceof Node && !surface.contains(event.target)) dismiss(); }
	function onKey(event: KeyboardEvent) { if (open && event.key === 'Escape') { event.stopPropagation(); dismiss(); } }
</script>

<svelte:window onpointerdown={onPointer} onkeydown={onKey} onresize={position} onscrollcapture={position} />

{#if open}
	<div class="popover" class:fill data-popover bind:this={surface} style={hidden ? `${style} visibility: hidden;` : style} role="dialog" aria-hidden={hidden || undefined}>{@render children()}</div>
{/if}

<style>
	.popover { position: fixed; z-index: 45; width: min(380px, calc(100vw - 24px)); max-height: min(70vh, 620px); overflow: auto; padding: 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--bg-elevated); box-shadow: var(--glass-elevation-2); }
	.popover.fill { display: flex; flex-direction: column; overflow: hidden; }
</style>
