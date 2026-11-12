<script module lang="ts">
	/** What a popover is anchored to: a rect, an element, or a reading of where the anchor is drawn now. */
	export type Anchor = DOMRect | HTMLElement | (() => DOMRect | null) | null;
	/** Fired on `window` when an anchor's text may have moved without a scroll or resize (an edit, or lines drawn after a scroll). */
	export const ANCHOR_MOVED = 'popover-anchor-moved';
	/** Where an anchor is drawn now. */
	export const anchorRect = (value: Anchor) => typeof value === 'function' ? value() : value instanceof HTMLElement ? value.getBoundingClientRect() : value;
</script>

<script lang="ts">
	import { tick } from 'svelte';
	import { portal } from '$lib/actions/portal';
	import { menuPop } from '$lib/popoverMotion';
	let { anchor, open, onClose, onClosed = () => {}, restoreFocus, placement = 'right', fill = false, width: cardWidth, onPlace, bounds = null, avoidRect, clearAnchor = false, initialFocus, takeFocus = true, surfaceRole = 'dialog', sideKey, keepOpenOnPointer, children }: {
		anchor: Anchor;
		open: boolean;
		onClose: () => void;
		onClosed?: () => void;
		restoreFocus?: () => void;
		placement?: 'right' | 'bottom' | 'bottom-end' | 'pointer';
		/** Make the content a column bounded by the popover, so one child can shrink and scroll while the rest stay in view. */
		fill?: boolean;
		/** A CSS width for a card that should span the column of text it covers, instead of the default card width. */
		width?: string;
		/** Called with the card each time it has been placed, so an owner can measure where it landed. */
		onPlace?: (card: HTMLElement) => void;
		/** Keep the surface inside this element or live rectangle instead of anywhere in the window. */
		bounds?: Anchor;
		/** A live rectangle the card should clear vertically when it cannot fit beside its anchor. */
		avoidRect?: () => DOMRect | null;
		/** Keep a thread's anchor visible while its contents grow. */
		clearAnchor?: boolean;
		/** A selector for what takes focus on open, when present; otherwise the first focusable element does. */
		initialFocus?: string;
		/** False leaves the focus where it is on open, for a card that opens beside text the person is editing. */
		takeFocus?: boolean;
		/** Omit the wrapper role when a child supplies the popup's semantic role, such as a listbox. */
		surfaceRole?: 'dialog' | null;
		/** What the surface is about, when that outlives the `anchor` value. A surface keeps its side until it closes, a scroll or resize moves the anchor, or this changes. */
		sideKey?: unknown;
		/** A related control that can be used without dismissing this surface. */
		keepOpenOnPointer?: (target: EventTarget | null) => boolean;
		children: import('svelte').Snippet;
	} = $props();
	let surface = $state<HTMLElement>();
	let opener = $state<HTMLElement | null>(null);
	let style = $state('left: 12px; top: 12px;');
	/** A live anchor whose text is not drawn, or is scrolled out of the area, hides its surface; it returns when the text does. Other activity leaves a surface holding focus in place, clamped into the area. */
	let hidden = $state(true);
	type Side = 'left' | 'right' | 'above' | 'below' | 'avoid-above' | 'avoid-below';
	let rememberedSide: Side | null = null;
	let rememberedPointer: { right: boolean; below: boolean } | null = null;
	let rememberedKey: unknown;
	let anchorSeen: DOMRect | null | undefined;
	let lastScrollInput = 0;
	/** The area the surface may occupy: the viewport, or the part of `bounds` inside it. */
	function region() {
		const edge = anchorRect(bounds);
		const viewport = window.visualViewport;
		const top = viewport?.offsetTop ?? 0;
		const bottom = top + (viewport?.height ?? window.innerHeight);
		return {
			left: Math.max(0, edge?.left ?? 0), top: Math.max(top, edge?.top ?? top),
			right: Math.min(window.innerWidth, edge?.right ?? window.innerWidth), bottom: Math.min(bottom, edge?.bottom ?? bottom),
		};
	}
	function position(event?: Event) {
		const rect = anchorRect(anchor);
		const live = typeof anchor === 'function';
		if (!surface) return;
		// A scroll inside the card or in another pane left the anchor where it was, so there is nothing to answer.
		if (event?.type === 'scroll' && event.target !== window.visualViewport && rect?.top === anchorSeen?.top && rect?.left === anchorSeen?.left) return;
		anchorSeen = rect;
		const active = document.activeElement;
		const typing = !!active && surface.contains(active) && active.matches('textarea, input, [contenteditable]:not([contenteditable="false"])');
		// While the person types in the card, a scroll counts as theirs only when a wheel or touch drove it; the document moving under them (an edit above, lines drawn) leaves the card in place.
		const moved = (event?.type === 'resize' && !typing) || (event?.type === 'scroll' && (!typing || performance.now() - lastScrollInput < 500));
		// Keep an active editor visible through viewport changes; a deliberate scroll can still hide it.
		const engaged = !moved && surface.contains(active);
		if (!rect) { if (live && !engaged) hidden = true; return; }
		const area = region();
		if (!engaged && (rect.bottom < area.top || rect.top > area.bottom || rect.right < area.left || rect.left > area.right)) {
			if (live) hidden = true; else dismiss();
			return;
		}
		hidden = false;
		// Measure the natural size: a width cap from an earlier placement must not shrink what a roomier one may use.
		const capped = surface.style.maxWidth;
		const heightCap = surface.style.maxHeight;
		surface.style.maxWidth = '';
		surface.style.maxHeight = '';
		// Layout dimensions are unaffected by the entrance scale; a transformed rect would permanently shrink the width cap.
		const naturalWidth = surface.offsetWidth;
		const naturalHeight = surface.offsetHeight;
		surface.style.maxWidth = capped;
		surface.style.maxHeight = heightCap;
		const gap = 8;
		const width = Math.min(naturalWidth, area.right - area.left - 2 * gap);
		const height = Math.min(naturalHeight, area.bottom - area.top - 2 * gap);
		const avoided = avoidRect?.();
		const under = placement === 'bottom-end' || (placement === 'bottom' && rect.left + width > area.right - gap) ? rect.right - width : rect.left;
		const spots: Record<Side, [number, number]> = {
			below: [under, rect.bottom + gap],
			above: [under, rect.top - height - gap],
			right: [rect.right + gap, rect.top],
			left: [rect.left - width - gap, rect.top],
			'avoid-below': [under, (avoided?.bottom ?? rect.bottom) + gap],
			'avoid-above': [under, (avoided?.top ?? rect.top) - height - gap],
		};
		// The side holds until the person moves the anchor: growing while they type clamps the surface rather than flipping it away from where they are looking.
		const key = sideKey ?? anchor;
		if (key !== rememberedKey || moved) { rememberedKey = key; rememberedSide = null; rememberedPointer = null; }
		// Clamping an editor whose anchor has scrolled out would cap it at zero height, taking Send with it: leave that surface to the placement below.
		if (clearAnchor && !(engaged && (rect.bottom < area.top || rect.top > area.bottom)) && (placement === 'pointer' || placement === 'bottom' || placement === 'bottom-end' || (rect.right + gap + width > area.right - gap && rect.left - gap - width < area.left + gap))) {
			const above = Math.max(0, rect.top - area.top - 2 * gap);
			const below = Math.max(0, area.bottom - rect.bottom - 2 * gap);
			// A card opened at a click keeps the pointer rule: below the line whenever it fits there.
			rememberedSide ??= (placement === 'pointer' && height <= below) || below >= above ? 'below' : 'above';
			const room = rememberedSide === 'above' ? above : below;
			// A composer being typed in keeps its own height: the anchor can move while the person types, and a band
			// below the card's own minimum clips the field and Send out of the card. The discussion regions are
			// already at their minimums, so what the composer sits on plus the composer is that floor; take the
			// tallest band the canvas has, bottom-pinned, and the discussion scrolls inside it.
			const pinned = engaged ? surface.querySelector<HTMLElement>('[data-autosize-sticky]') : null;
			const need = pinned ? pinned.getBoundingClientRect().bottom - surface.getBoundingClientRect().top + gap : 0;
			const band = area.bottom - area.top - 2 * gap;
			if (pinned && need > room && band > need) {
				const cap = Math.max(need, height);
				const fittedHeight = Math.min(naturalHeight, cap);
				const top = Math.max(area.top + gap, area.bottom - gap - fittedHeight);
				const left = Math.max(area.left + gap, Math.min(under, area.right - width - gap));
				style = `left: ${left}px; top: ${top}px; max-width: ${width}px; max-height: ${cap}px;`;
				if (onPlace) void tick().then(() => requestAnimationFrame(() => { if (surface) onPlace(surface); }));
				return;
			}
			const fittedHeight = Math.min(height, room);
			const top = rememberedSide === 'above' ? rect.top - gap - fittedHeight : rect.bottom + gap;
			const left = Math.max(area.left + gap, Math.min(under, area.right - width - gap));
			style = `left: ${left}px; top: ${top}px; max-width: ${width}px; max-height: min(70vh, 620px, ${room}px);`;
			if (onPlace) void tick().then(() => requestAnimationFrame(() => { if (surface) onPlace(surface); }));
			return;
		}
		if (placement === 'pointer') {
			// Opened by a click: `rect` is the clicked line's box at the pointer's x. The card's top sits `--space-1` under that box (the line's own half-leading, so no glyph of the next line peeks out) and to the pointer's right; near the right edge it flips left, near the bottom it flips above the line.
			const reach = 12, drop = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--space-1')) || 4;
			rememberedPointer ??= { right: rect.right + reach + width <= area.right - gap, below: rect.bottom + drop + height <= area.bottom - gap };
			const beside = rememberedPointer.right ? rect.right + reach : rect.left - reach - width;
			const down = rememberedPointer.below ? rect.bottom + drop : rect.top - drop - height;
			const top = Math.max(area.top + gap, Math.min(down, area.bottom - height - gap));
			style = `left: ${Math.max(area.left + gap, Math.min(beside, area.right - width - gap))}px; top: ${top}px; max-width: ${width}px; max-height: min(70vh, 620px, ${Math.max(0, area.bottom - top - gap)}px);`;
			if (onPlace) void tick().then(() => requestAnimationFrame(() => { if (surface) onPlace(surface); }));
			return;
		}
		const overflowsBelow = rect.bottom + gap + height > area.bottom - gap;
		const fitsAbove = rect.top - gap - height >= area.top + gap;
		let side: Side = 'below';
		if (placement === 'bottom' || placement === 'bottom-end') {
			// Below the anchor when it fits; otherwise above it.
			if (overflowsBelow && fitsAbove) side = 'above';
		} else if (rect.right + gap + width <= area.right - gap) {
			// Beside the anchor, never over it.
			side = 'right';
		} else if (rect.left - gap - width >= area.left + gap) {
			side = 'left';
		} else if (avoided && avoided.bottom + gap + height <= area.bottom - gap) {
			// For an image region, use empty canvas below the image before covering another tag.
			side = 'avoid-below';
		} else if (avoided && avoided.top - gap - height >= area.top + gap) {
			// When the lower canvas is full, use the empty canvas above the image instead.
			side = 'avoid-above';
		} else if (overflowsBelow && fitsAbove) {
			// No room beside or outside the avoided rectangle: above the anchor. Otherwise it stays below it, clamped into the canvas.
			side = 'above';
		}
		rememberedSide ??= side;
		let [left, top] = spots[rememberedSide];
		top = Math.max(area.top + gap, Math.min(top, area.bottom - height - gap));
		left = Math.max(area.left + gap, Math.min(left, area.right - width - gap));
		style = `left: ${left}px; top: ${top}px; max-width: ${width}px; max-height: min(70vh, 620px, ${Math.max(0, area.bottom - top - gap)}px);`;
		// The style lands on the card in the frame after it is set: the owner measures it then.
		if (onPlace) void tick().then(() => requestAnimationFrame(() => { if (surface) onPlace(surface); }));
	}
	$effect(() => {
		if (!open) { rememberedSide = null; rememberedPointer = null; return; }
		window.addEventListener(ANCHOR_MOVED, position);
		const viewport = window.visualViewport;
		viewport?.addEventListener('resize', position);
		viewport?.addEventListener('scroll', position);
		return () => { window.removeEventListener(ANCHOR_MOVED, position); viewport?.removeEventListener('resize', position); viewport?.removeEventListener('scroll', position); };
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
			if (cancelled || !open || hidden || !takeFocus) return;
			const preferred = initialFocus ? surface?.querySelector<HTMLElement>(initialFocus) : null;
			(preferred ?? surface?.querySelector<HTMLElement>('button, input, textarea, select, [tabindex]:not([tabindex="-1"])'))?.focus();
		});
		return () => { cancelled = true; };
	});
	// Content growing inside the card (a reply being typed) clamps it again, so its foot and Send stay on screen.
	$effect(() => {
		if (!open || !surface) return;
		const observer = new ResizeObserver(() => position());
		observer.observe(surface);
		return () => observer.disconnect();
	});
	// A new anchor (a card that stops following its text) is placed at once, not at the next scroll or edit.
	$effect(() => {
		void anchor;
		void sideKey;
		if (open) void tick().then(() => position());
	});
	function onScrollInput(event: Event) { if (!(event.target instanceof Node && surface?.contains(event.target))) lastScrollInput = performance.now(); }
	function dismiss() { onClose(); if (restoreFocus) restoreFocus(); else opener?.focus(); }
	function onPointer(event: PointerEvent) { if (open && surface && event.target instanceof Node && !surface.contains(event.target) && !keepOpenOnPointer?.(event.target)) dismiss(); }
	function onKey(event: KeyboardEvent) { if (open && event.key === 'Escape') { event.stopPropagation(); dismiss(); } }
</script>

<svelte:window onpointerdown={onPointer} onkeydown={onKey} onresize={position} onscrollcapture={position} onwheelcapture={onScrollInput} ontouchmovecapture={onScrollInput} />

{#if open}
	<!-- Portaled to `document.body` so no pane clips it or paints over it; `bounds` still keeps it on its canvas.
	     Clipping keeps it measurable without inherited visibility transitions delaying focus. -->
	<div class="popover" class:fill data-popover use:portal in:menuPop|global out:menuPop onoutroend={onClosed} onkeydown={onKey} bind:this={surface} style={`${cardWidth ? `width: ${cardWidth}; ` : ''}${hidden ? `${style} clip-path: inset(100%);` : style}`} role={surfaceRole ?? undefined} aria-hidden={hidden || undefined} inert={hidden}>{@render children()}</div>
{/if}

<style>
	.popover { position: fixed; z-index: 45; box-sizing: border-box; width: min(380px, calc(100vw - 24px)); max-height: min(70vh, 620px); overflow: auto; padding: 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--bg-elevated); box-shadow: var(--glass-elevation-2); }
	/* Placement and clipping update immediately; menuPop owns the entrance and exit. */
	.popover { transition-property: none; }
	.popover.fill { display: flex; flex-direction: column; overflow: auto; }
</style>
