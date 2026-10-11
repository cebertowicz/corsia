<script lang="ts">
	import { cubicOut } from 'svelte/easing';
	import { onMount, tick } from 'svelte';
	import { fade, fly } from 'svelte/transition';
	import type { Snippet } from 'svelte';
	import { focusTrap } from '$lib/actions/focusTrap';
	import { portal } from '$lib/actions/portal';
	import { pushBackLayer } from '$lib/historyBackLayer';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { toastClearance } from '$lib/stores/toastClearance';

	let {
		ariaLabel,
		heading = null,
		headingMeta = null,
		leading,
		doneLabel = 'Done',
		onClose,
		children,
		initialFocusSelector = '[data-drawer-autofocus]',
		fill = false,
	}: {
		ariaLabel: string;
		/**
		 * Optional chrome-level title. Omit it when the drawer's content already
		 * carries its own titled header, so the two never say the same thing twice.
		 */
		heading?: string | null;
		/** A short fact beside the title, such as a count; shown smaller in the accent colour. */
		headingMeta?: string | null;
		/** An action at the leading edge; with one, the title centres between it and Done. */
		leading?: Snippet;
		/** Visible text of the dismiss control; it is also its accessible name. */
		doneLabel?: string;
		onClose: () => void;
		children: Snippet;
		initialFocusSelector?: string;
		/** Make the content a column bounded by the drawer, so one child can shrink and scroll while the rest stay in view. */
		fill?: boolean;
	} = $props();

	let panel = $state<HTMLElement | null>(null);
	let dragY = $state(0);
	let dragging = $state(false);
	let dragPointer: number | null = null;
	let dragStartY = 0;
	let dragStartedAt = 0;
	let stopToastClearance: (() => void) | undefined;

	const flyParams = $derived(
		$prefersReducedMotion
			? { y: 0, duration: 0 }
			: { y: 72, duration: 260, easing: cubicOut },
	);
	const fadeParams = $derived({ duration: $prefersReducedMotion ? 0 : 160 });

	function onBackdrop(event: MouseEvent): void {
		if (event.target === event.currentTarget) onClose();
	}

	function startDrag(event: PointerEvent): void {
		if (event.button !== 0) return;
		const handle = event.currentTarget as HTMLElement;
		dragPointer = event.pointerId;
		dragStartY = event.clientY;
		dragStartedAt = performance.now();
		dragY = 0;
		dragging = true;
		handle.setPointerCapture?.(event.pointerId);
	}

	function moveDrag(event: PointerEvent): void {
		if (!dragging || dragPointer !== event.pointerId) return;
		dragY = Math.max(0, event.clientY - dragStartY);
	}

	function finishDrag(event: PointerEvent): void {
		if (!dragging || dragPointer !== event.pointerId) return;
		const elapsed = Math.max(1, performance.now() - dragStartedAt);
		const velocity = dragY / elapsed;
		const threshold = Math.min(180, Math.max(96, (panel?.clientHeight ?? 480) * 0.24));
		dragging = false;
		dragPointer = null;
		if (dragY >= threshold || (dragY >= 64 && velocity > 0.75)) {
			onClose();
			return;
		}
		dragY = 0;
	}

	function restingViewportBottomOccupancy(node: HTMLElement): number {
		const bottomMargin = Number.parseFloat(getComputedStyle(node).marginBottom) || 0;
		return node.offsetHeight + bottomMargin;
	}

	function releaseToastClearance(): void {
		stopToastClearance?.();
		stopToastClearance = undefined;
	}

	onMount(() => {
		stopToastClearance = panel
			? toastClearance.register(panel, {
				measure: () => restingViewportBottomOccupancy(panel),
			})
			: undefined;
		const removeBackLayer = pushBackLayer(() => {
			onClose();
			return true;
		});
		let mounted = true;
		void tick().then(() => {
			if (!mounted || !panel?.isConnected) return;
			(panel.querySelector<HTMLElement>(initialFocusSelector) ?? panel).focus();
		});
		return () => {
			mounted = false;
			releaseToastClearance();
			removeBackLayer();
		};
	});
</script>

<!--
	The backdrop is portaled to `document.body` so no ancestor can clip it, lift
	or sink it, or decide its stacking by a `:has()` rule. Mounted inside the chat
	band it was painted only over the chat column in a WebKit webview, leaving the
	rail, the open document and the right pane uncovered.
-->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="drawer-backdrop"
	data-adaptive-drawer
	onclick={onBackdrop}
	onoutrostart={releaseToastClearance}
	transition:fade={fadeParams}
	use:portal
>
	<div class="drawer-motion" transition:fly={flyParams}>
		<div
			bind:this={panel}
			class="drawer-panel"
			class:dragging
			style:--drawer-drag-y="{dragY}px"
			role="dialog"
			aria-modal="true"
			aria-label={ariaLabel}
			tabindex="-1"
			use:focusTrap={{ onEscape: onClose, autoFocus: false }}
		>
			<div class="drawer-chrome">
				<div
					class="drawer-drag-region"
					aria-hidden="true"
					onpointerdown={startDrag}
					onpointermove={moveDrag}
					onpointerup={finishDrag}
					onpointercancel={finishDrag}
				>
					<span class="drawer-grabber"></span>
				</div>
				<div class="drawer-head" class:centered={!!leading}>
					{#if leading}<span class="drawer-lead">{@render leading()}</span>{/if}
					{#if heading}<h2>{heading}{#if headingMeta}{' '}<span class="drawer-head-meta">{headingMeta}</span>{/if}</h2>{:else}<span class="drawer-head-spacer"></span>{/if}
					<button class="drawer-done" type="button" onclick={onClose}>{doneLabel}</button>
				</div>
			</div>
			<div class="drawer-body" class:fill>
				{@render children()}
			</div>
		</div>
	</div>
</div>

<style>
	.drawer-backdrop {
		position: fixed;
		inset: 0;
		/* Canonical ladder (DESIGN-SYSTEM.md §2.8): drawers sit at 20-30, below
		   Modal's 100 and Cmd palette's 70, so both can still open and receive
		   focus over an open drawer. */
		z-index: 28;
		display: flex;
		align-items: flex-end;
		justify-content: center;
		background: color-mix(in srgb, var(--glass-scrim) 88%, transparent);
		overscroll-behavior: contain;
		pointer-events: auto;
	}
	.drawer-motion {
		width: 100%;
		max-width: 840px;
		padding: 0 max(8px, env(safe-area-inset-right, 0px)) 0 max(8px, env(safe-area-inset-left, 0px));
		box-sizing: border-box;
	}
	.drawer-panel {
		--drawer-drag-y: 0px;
		width: 100%;
		max-height: min(82dvh, calc(100dvh - max(16px, env(safe-area-inset-top, 0px))));
		display: flex;
		flex-direction: column;
		overflow: hidden;
		transform: translateY(var(--drawer-drag-y));
		transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1);
		border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
		border-bottom: 0;
		border-radius: 28px 28px 0 0;
		/* `--surface-1` never existed, so the browser dropped this entire
		   declaration and the drawer became literally transparent. Keep the Liquid
		   Glass depth with the canonical material token and a real blurred fill. */
		background: var(--glass-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		box-shadow: 0 -18px 64px rgba(0, 0, 0, 0.32), var(--glass-rim);
	}
	.drawer-panel.dragging {
		transition: none;
	}
	.drawer-panel:focus {
		outline: none;
	}
	/*
	 * The canonical drawer header, promoted from the artifact tab sheet because
	 * its padding, spacing and text "Done" affordance read better than a corner
	 * close glyph. Every drawer consumer gets this same chrome; only the optional
	 * title varies.
	 */
	.drawer-chrome {
		position: relative;
		flex: none;
		display: flex;
		flex-direction: column;
		padding: 8px 12px 0;
	}
	.drawer-drag-region {
		display: grid;
		place-items: start center;
		padding-bottom: 8px;
		cursor: grab;
		touch-action: none;
	}
	.drawer-drag-region:active {
		cursor: grabbing;
	}
	.drawer-grabber {
		width: 36px;
		height: 4px;
		margin-top: 2px;
		border-radius: var(--radius-pill);
		background: var(--border);
	}
	.drawer-head {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 4px 6px;
	}
	.drawer-head h2 {
		flex: 1;
		margin: 0;
		font-size: var(--text-subhead);
		font-weight: var(--fw-semibold);
		color: var(--text-secondary);
	}
	.drawer-head-meta {
		margin-left: 4px;
		font-size: var(--text-caption);
		font-weight: var(--fw-semibold);
		font-variant-numeric: tabular-nums;
		color: var(--accent);
	}
	/* A leading action centres the title between it and Done. */
	.drawer-head.centered { display: grid; grid-template-columns: 1fr minmax(0, auto) 1fr; }
	.drawer-head.centered h2 {
		min-width: 0;
		overflow: hidden;
		text-align: center;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.drawer-lead { justify-self: start; }
	.drawer-head.centered .drawer-done { justify-self: end; }
	/* Holds Done at the trailing edge when the content owns the title. */
	.drawer-head-spacer { flex: 1; }
	.drawer-done {
		font: inherit;
		font-size: var(--text-subhead);
		font-weight: var(--fw-semibold);
		color: var(--accent);
		background: none;
		border: none;
		padding: 6px 8px;
		cursor: pointer;
	}
	.drawer-done:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
		border-radius: var(--radius-sm);
	}
	.drawer-body {
		min-height: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
		scrollbar-gutter: stable;
		padding: 0 var(--space-4) max(var(--space-4), env(safe-area-inset-bottom, 0px));
	}
	.drawer-body.fill { display: flex; flex-direction: column; overflow: hidden; }
	@media (max-width: 620px) {
		/* Touch minimum for the only dismiss control in the chrome. */
		.drawer-done { min-height: 44px; }
	}
	@media (min-width: 769px) {
		.drawer-motion {
			padding-left: 16px;
			padding-right: 16px;
		}
		.drawer-panel {
			border-bottom: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
			border-radius: 28px;
			margin-bottom: max(16px, env(safe-area-inset-bottom, 0px));
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.drawer-panel {
			transition: none;
		}
	}
</style>
