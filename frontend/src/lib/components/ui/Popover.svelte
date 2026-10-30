<script module lang="ts">
	/** What a popover is anchored to: a rect, an element, or a reading of where the anchor is drawn now. */
	export type Anchor = DOMRect | HTMLElement | (() => DOMRect | null) | null;
	/** Fired on `window` when an anchor's text may have moved without a scroll or resize (an edit, or lines drawn after a scroll). */
	export const ANCHOR_MOVED = 'popover-anchor-moved';
</script>

<script lang="ts">
	import { tick } from 'svelte';
	import { portal } from '$lib/actions/portal';
	import { menuPop } from '$lib/popoverMotion';
	let { anchor, open, onClose, onClosed = () => {}, restoreFocus, placement = 'right', fill = false, width: cardWidth, onPlace, bounds = null, avoidRect, initialFocus, surfaceRole = 'dialog', children }: {
		anchor: Anchor;
		open: boolean;
		onClose: () => void;
		onClosed?: () => void;
		restoreFocus?: () => void;
		placement?: 'right' | 'bottom' | 'bottom-end';
		/** Make the content a column bounded by the popover, so one child can shrink and scroll while the rest stay in view. */
		fill?: boolean;
		/** A CSS width for a card that should span the column of text it covers, instead of the default card width. */
		width?: string;
		/** Called with the card each time it has been placed, so an owner can measure where it landed. */
		onPlace?: (card: HTMLElement) => void;
		/** Keep the surface inside this element (the canvas it belongs to) instead of anywhere in the window. */
		bounds?: HTMLElement | null;
		/** A live rectangle the card should clear vertically when it cannot fit beside its anchor. */
		avoidRect?: () => DOMRect | null;
		/** A selector for what takes focus on open, when present; otherwise the first focusable element does. */
		initialFocus?: string;
		/** Omit the wrapper role when a child supplies the popup's semantic role, such as a listbox. */
		surfaceRole?: 'dialog' | null;
		children: import('svelte').Snippet;
	} = $props();
	let surface = $state<HTMLElement>();
	let opener = $state<HTMLElement | null>(null);
	let style = $state('left: 12px; top: 12px;');
	/** A live anchor whose text is not drawn, or is scrolled out of the area, hides its surface; it returns when the text does. */
	let hidden = $state(true);
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
		// Layout dimensions are unaffected by the entrance scale; a transformed rect would permanently shrink the width cap.
		const naturalWidth = surface.offsetWidth;
		const naturalHeight = surface.offsetHeight;
		surface.style.maxWidth = capped;
		const gap = 8;
		const width = Math.min(naturalWidth, area.right - area.left - 2 * gap);
		const height = Math.min(naturalHeight, area.bottom - area.top - 2 * gap);
		const avoided = avoidRect?.();
		let left = rect.left;
		let top = rect.bottom + gap;
		if (placement === 'bottom' || placement === 'bottom-end') {
			if (placement === 'bottom-end' || left + width > area.right - gap) left = rect.right - width;
			// Below the anchor when it fits; otherwise above it, never clamped up over the anchor.
			if (top + height > area.bottom - gap && rect.top - gap - height >= area.top + gap) top = rect.top - height - gap;
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
		// The style lands on the card in the frame after it is set: the owner measures it then.
		if (onPlace) void tick().then(() => requestAnimationFrame(() => { if (surface) onPlace(surface); }));
	}
	$effect(() => {
		if (!open) return;
		window.addEventListener(ANCHOR_MOVED, position);
		return () => window.removeEventListener(ANCHOR_MOVED, position);
	});
	$effect(() => {
		if (!open) return;
		hidden = true;
		let cancelled = false;
		opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		void tick().then(async () => {
			if (cancelled) return;
			position();
			await tick(); // Apply placement and visibility before moving focus into the card.
			if (cancelled || !open || hidden) return;
			const preferred = initialFocus ? surface?.querySelector<HTMLElement>(initialFocus) : null;
			(preferred ?? surface?.querySelector<HTMLElement>('button, input, textarea, select, [tabindex]:not([tabindex="-1"])'))?.focus();
		});
		return () => { cancelled = true; };
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
	<!-- Portaled to `document.body` so no pane clips it or paints over it; `bounds` still keeps it on its canvas.
	     Clipping keeps it measurable without inherited visibility transitions delaying focus. -->
	<div class="popover" class:fill data-popover use:portal in:menuPop|global out:menuPop onoutroend={onClosed} bind:this={surface} style={`${cardWidth ? `width: ${cardWidth}; ` : ''}${hidden ? `${style} clip-path: inset(100%);` : style}`} role={surfaceRole ?? undefined} aria-hidden={hidden || undefined} inert={hidden}>{@render children()}</div>
{/if}

<style>
	.popover { position: fixed; z-index: 45; box-sizing: border-box; width: min(380px, calc(100vw - 24px)); max-height: min(70vh, 620px); overflow: auto; padding: 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--bg-elevated); box-shadow: var(--glass-elevation-2); }
	/* Placement and clipping update immediately; menuPop owns the entrance and exit. */
	.popover { transition-property: none; }
	.popover.fill { display: flex; flex-direction: column; overflow: hidden; }
</style>
