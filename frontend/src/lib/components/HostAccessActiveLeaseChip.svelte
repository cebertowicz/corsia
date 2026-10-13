<script lang="ts">
	import AdaptiveDrawer from '$lib/components/ui/AdaptiveDrawer.svelte';
	import HostAccessActiveLeaseDrawer from './HostAccessActiveLeaseDrawer.svelte';
	import { owner, remaining } from '$lib/hostAccess/leaseFormat';
	import type { HostAccessLease } from '$lib/api/hostAccess';

	/**
	 * One active lease, as the compact shield-and-countdown chip that occupies
	 * the attention rail's narrow slot, plus the drawer it opens. Open state
	 * stays with the control that owns it, so only one chip is open at a time
	 * and focus can be restored to the trigger after the drawer closes.
	 */
	let {
		lease,
		nowMs,
		open,
		deciding,
		disabled,
		onOpen,
		onClose,
		onRevoke,
	}: {
		lease: HostAccessLease;
		nowMs: number;
		open: boolean;
		deciding: boolean;
		disabled: boolean;
		onOpen: (event: MouseEvent) => void;
		onClose: () => void;
		onRevoke: (event: MouseEvent) => void;
	} = $props();
</script>

<div class="active-chip">
	<button
		type="button"
		class="active-chip-trigger"
		aria-label="Host access"
		aria-haspopup="dialog"
		aria-expanded={open}
		onclick={onOpen}
	>
		<svg
			class="host-access-icon"
			aria-hidden="true"
			width="14"
			height="14"
			viewBox="0 0 24 24"
			fill="none"
			stroke="currentColor"
			stroke-width="1.8"
			stroke-linecap="round"
			stroke-linejoin="round"
		>
			<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" />
			<path d="M12 8v4" />
			<path d="M12 16h.01" />
		</svg>
		<span class="countdown" aria-hidden="true">{remaining(lease, nowMs)}</span>
	</button>
	{#key lease.id}
		<AdaptiveDrawer ariaLabel={`Host access — ${owner(lease)}`} {open} onClose={onClose}>
			<HostAccessActiveLeaseDrawer
				{lease}
				{nowMs}
				{deciding}
				{disabled}
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
	.active-chip-trigger {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		min-height: 32px;
		padding: 0 var(--space-2);
		border: 1px solid var(--amber-line);
		border-radius: var(--radius-pill);
		background: var(--bg-secondary);
		/* The lease track scrolls, so it clips any glow; the border carries the edge. */
		box-shadow: none;
		font: inherit;
		font-size: var(--text-footnote);
		color: var(--amber);
		cursor: pointer;
	}
	.active-chip-trigger:focus-visible {
		outline: 2px solid var(--violet-line);
		outline-offset: 2px;
	}
	.host-access-icon {
		flex-shrink: 0;
		color: var(--amber);
	}
	.countdown {
		font-variant-numeric: tabular-nums;
		color: var(--amber);
		min-width: 3.2em;
		text-align: right;
	}

	@media (max-width: 720px) {
		.active-chip-trigger { min-height: 44px; }
	}
</style>
