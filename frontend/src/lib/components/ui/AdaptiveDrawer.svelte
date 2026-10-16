<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { fade, fly } from 'svelte/transition';
	import type { Snippet } from 'svelte';
	import { focusTrap } from '$lib/actions/focusTrap';
	import { portal } from '$lib/actions/portal';
	import { settleHeight } from '$lib/actions/settleHeight';
	import { trackColumnBox, type ColumnBox } from '$lib/actions/trackColumnBox';
	import { pushBackLayer } from '$lib/historyBackLayer';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { sheetFly } from '$lib/sheetMotion';
	import { createSheetDrag } from '$lib/sheetDrag.svelte';

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
		followKeyboard = false,
		headless = false,
		opaque = false,
		modal = true,
		within = null,
		detent = 'full',
		onDetent,
		open = true,
		onClosed = () => {},
		covered = false,
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
		/**
		 * Sit on the visible part of the screen, above an on-screen keyboard, for a
		 * drawer whose text field must stay in view while typing. It reads the
		 * visual viewport the app already publishes as CSS variables.
		 */
		followKeyboard?: boolean;
		/** Content may own the header, leaving the chrome with its grabber only. */
		headless?: boolean;
		/** Use over media or dense content where the glass material hurts reading. */
		opaque?: boolean;
		/** Keep the sheet interactive without blocking controls outside it. */
		modal?: boolean;
		/** Constrain this sheet and its scrim to a document column rather than the viewport. */
		within?: HTMLElement | null;
		detent?: 'full' | 'half';
		onDetent?: (detent: 'full' | 'half') => void;
		/** Whether the caller keeps the drawer mounted for its local close transition. */
		open?: boolean;
		/** Runs after the drawer's 260ms local outro completes. */
		onClosed?: () => void;
		/**
		 * Something opened from the drawer covers what it belongs to (an image over its document): the drawer stays open
		 * underneath, its place kept, but out of sight and reach, with no scrim over what covers it, until uncovered.
		 */
		covered?: boolean;
	} = $props();

	let box = $state<ColumnBox | null>(null);
	$effect(() => {
		if (!within) { box = null; return; }
		return trackColumnBox(within, (next) => { box = next; });
	});
	let panel = $state<HTMLElement | null>(null);
	const drag = createSheetDrag({
		detent: () => detent,
		height: () => panel?.clientHeight ?? 480,
		onExpand: () => onDetent?.('full'),
		onDismiss: () => onClose(),
	});

	// Reduced motion crossfades over --dur-tight instead of sliding: the sheet is never cut in or out.
	// The token is read once on mount, never inside the derivation.
	let tightMs = $state(120);
	onMount(() => { tightMs = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur-tight')) || 120; });
	const fadeParams = $derived({ duration: $prefersReducedMotion ? tightMs : 160 });
	// The sheet's outro ends in onClosed, so under reduced motion it holds still for the crossfade instead of ending at once.
	const flyParams = $derived(sheetFly($prefersReducedMotion, tightMs));

	function onBackdrop(event: MouseEvent): void {
		if (modal && detent !== 'half' && event.target === event.currentTarget) onClose();
	}

	$effect(() => {
		if (!open || !panel) return;
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
{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
	class="drawer-backdrop"
	class:follow-keyboard={followKeyboard}
	class:within={!!box}
	class:half={detent === 'half'}
	class:nonmodal={!modal || detent === 'half'}
	class:covered
	inert={covered}
	style:--drawer-top="{box?.top ?? 0}px"
	style:--drawer-right="{box?.right ?? 0}px"
	style:--drawer-bottom="{box?.bottom ?? 0}px"
	style:--drawer-left="{box?.left ?? 0}px"
	style:--drawer-h="{box?.height ?? 0}px"
	data-adaptive-drawer
	onclick={onBackdrop}
		in:fade|global={fadeParams}
		out:fade={fadeParams}
		use:portal
	>
		<div class="drawer-motion" in:fly|global={flyParams} out:fly={flyParams} onoutroend={onClosed}>
		<div
			bind:this={panel}
			class="drawer-panel"
			class:dragging={drag.dragging}
			class:opaque
			class:half={detent === 'half'}
			style:--drawer-drag-y="{drag.y}px"
			role="dialog"
			aria-modal={modal && detent !== 'half' ? 'true' : 'false'}
			aria-label={ariaLabel}
			tabindex="-1"
			use:focusTrap={{ onEscape: onClose, autoFocus: false }}
			use:settleHeight={{ enabled: open && !drag.dragging, reduced: $prefersReducedMotion }}
		>
			<div class="drawer-chrome">
				<div
					class="drawer-drag-region"
					aria-hidden="true"
					onpointerdown={drag.start}
					onpointermove={drag.move}
					onpointerup={drag.finish}
					onpointercancel={drag.finish}
				>
					<span class="drawer-grabber"></span>
				</div>
				{#if !headless}<div class="drawer-head" class:centered={!!leading}>
					{#if leading}<span class="drawer-lead">{@render leading()}</span>{/if}
					{#if heading}<h2>{heading}{#if headingMeta}{' '}<span class="drawer-head-meta">{headingMeta}</span>{/if}</h2>{:else}<span class="drawer-head-spacer"></span>{/if}
					<button class="drawer-done" type="button" onclick={onClose}>{doneLabel}</button>
				</div>{/if}
			</div>
			<div class="drawer-body" class:fill>
				{@render children()}
			</div>
		</div>
	</div>
	</div>
{/if}

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
		transition: background-color var(--dur-standard) var(--ease-entrance);
	}
	.drawer-backdrop.within { inset: var(--drawer-top) var(--drawer-right) var(--drawer-bottom) var(--drawer-left); background: color-mix(in srgb, var(--glass-scrim) 88%, transparent); }
	.drawer-backdrop.half { background-color: transparent; backdrop-filter: none; -webkit-backdrop-filter: none; }
	.drawer-backdrop.nonmodal { background: none; backdrop-filter: none; -webkit-backdrop-filter: none; pointer-events: none; }
	.drawer-backdrop.nonmodal .drawer-motion { pointer-events: auto; }
	.drawer-backdrop.covered { visibility: hidden; }
	.within .drawer-motion { max-width: none; padding: 0; }
	.within .drawer-panel { max-height: calc(var(--drawer-h) * 0.82); }
	.drawer-backdrop.follow-keyboard {
		bottom: auto;
		top: var(--doc-visual-viewport-offset, 0px);
		height: var(--doc-visual-viewport-height, 100dvh);
	}
	.follow-keyboard .drawer-panel {
		max-height: min(82dvh, calc(var(--doc-visual-viewport-height, 100dvh) - max(16px, var(--safe-top))));
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
		max-height: min(82dvh, calc(100dvh - max(16px, var(--safe-top))));
		display: flex;
		flex-direction: column;
		overflow: hidden;
		transform: translateY(var(--drawer-drag-y));
		transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1), max-height var(--dur-standard) var(--ease-entrance);
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
	.drawer-panel.opaque { background: var(--bg-secondary); backdrop-filter: none; -webkit-backdrop-filter: none; }
	.drawer-panel.half { max-height: min(46dvh, calc(100dvh - max(16px, var(--safe-top)))); }
	.within .drawer-panel.half { max-height: calc(var(--drawer-h) * 0.46); }
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
	.drawer-panel.half .drawer-drag-region { padding-bottom: var(--space-3); }
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
		color: var(--violet-text);
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
		color: var(--violet-text);
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
