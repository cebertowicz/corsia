<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import HostAccessActiveLeaseDrawer from './HostAccessActiveLeaseDrawer.svelte';
	import { minutesLeft, owner } from '$lib/hostAccess/leaseFormat';
	import type { HostAccessTimed } from '$lib/api/hostAccess';

	/**
	 * One active approval, as the composer-style chip that names it in words with
	 * its time remaining ("Host access · 15 min left", "Saved login · 15 min left"), plus the
	 * drawer it opens. The words are its accessible name, time included. Open state
	 * stays with the control that owns it, so only one chip is open at a time
	 * and focus can be restored to the trigger after the drawer closes.
	 */
	let {
		lease,
		nowMs,
		open,
		deciding,
		disabled,
		notice = null,
		onOpen,
		onClose,
		onRevoke,
	}: {
		lease: HostAccessTimed;
		nowMs: number;
		open: boolean;
		deciding: boolean;
		disabled: boolean;
		/** The server's line when it refused the last Revoke. */
		notice?: string | null;
		onOpen: (event: MouseEvent) => void;
		onClose: () => void;
		onRevoke: (event: MouseEvent) => void;
	} = $props();

	const fill = $derived(lease.kind === 'fill');
</script>

<div class="active-chip">
	<button
		type="button"
		class="active-chip-trigger"
		aria-haspopup="dialog"
		aria-expanded={open}
		onclick={onOpen}
	>
		<!-- One inline run, so the space between the words and the time stays in the accessible name. -->
		<span class="chip-text">{fill ? 'Saved login' : 'Host access'} · <span class="countdown">{minutesLeft(lease, nowMs)}</span></span>
	</button>
	{#key lease.id}
		<AdaptiveDrawer ariaLabel={`${fill ? 'Saved login' : 'Host access'} — ${owner(lease)}`} flushBottom opaque {open} onClose={onClose}>
			<HostAccessActiveLeaseDrawer
				{lease}
				{nowMs}
				{deciding}
				{disabled}
				{notice}
				onRevoke={onRevoke}
			/>
		</AdaptiveDrawer>
	{/key}
</div>

<style>
	.active-chip {
		flex: 0 0 auto;
		pointer-events: auto;
	}
	/* The composer chip (DESIGN-SYSTEM §0.1): no fill and no border, secondary text. */
	.active-chip-trigger {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		min-height: 32px;
		padding: var(--space-1) var(--chip-inline-padding);
		border: none;
		border-radius: var(--radius-sm);
		background: none;
		font: inherit;
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
		color: var(--text-secondary);
		white-space: nowrap;
		cursor: pointer;
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.active-chip-trigger:hover, .active-chip-trigger[aria-expanded='true'] {
		color: var(--text-primary);
		background: color-mix(in srgb, var(--bg-hover) 70%, transparent);
	}
	.active-chip-trigger:focus-visible {
		outline: 2px solid var(--accent-line);
		outline-offset: 2px;
	}
	.countdown {
		display: inline-block;
		font-variant-numeric: tabular-nums;
		min-width: 3.2em;
		text-align: right;
	}

	@media (max-width: 720px) {
		.active-chip-trigger { min-height: 44px; }
	}
</style>
