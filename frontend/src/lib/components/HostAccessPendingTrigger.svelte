<script lang="ts">
	import { StatePill } from '$lib/components/ui';
	import { duration, owner, pendingLabel } from '$lib/hostAccess/leaseFormat';
	import type { HostAccessItem } from '$lib/api/hostAccess';

	/** The strip chip that opens a pending request's drawer. */
	let {
		request,
		stale,
		deciding = false,
		open,
		onclick,
		element = $bindable(null),
	}: {
		request: HostAccessItem;
		stale: boolean;
		/** This request's decision is in flight; a closed drawer must still show it. */
		deciding?: boolean;
		open: boolean;
		onclick: () => void;
		/** The button, so the control can return focus to it when the drawer closes. */
		element?: HTMLButtonElement | null;
	} = $props();
</script>

<button
	type="button"
	class="pending-request-trigger"
	aria-label={pendingLabel(request)}
	aria-haspopup="dialog"
	aria-expanded={open}
	aria-busy={request.kind === 'script' && deciding}
	bind:this={element}
	{onclick}
>
	{#if request.kind === 'script'}
		<span class="eyebrow sentence">Script approval</span>
		<span class="pending-owner">{request.script_name}</span>
		<span class="pending-status" class:stale class:deciding>{stale ? 'Not current' : deciding ? 'Saving…' : 'Needs approval'}</span>
	{:else}
		<span class="eyebrow">Temporary host access</span>
		<span class="pending-owner">{owner(request)}</span>
		<StatePill state={stale ? 'error' : 'warning'} label={stale ? 'Not current' : duration(request.requested_duration_seconds)} size="sm" />
	{/if}
	<span class="pending-action">Review →</span>
</button>

<style>
	.pending-request-trigger {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		width: 100%;
		min-height: var(--touch-target);
		padding: var(--space-2) var(--space-3);
		border: 1px solid var(--amber-line);
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--amber) 10%, var(--bg-secondary));
		color: var(--text-primary);
		font: inherit;
		text-align: left;
		cursor: pointer;
		pointer-events: auto;
	}
	.pending-request-trigger:focus-visible {
		outline: 2px solid var(--accent-line);
		outline-offset: 2px;
	}
	.pending-owner {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
		text-overflow: ellipsis;
		/* HostAccessLeaseControl's phone breakpoint sets these two. */
		white-space: var(--host-access-owner-wrap, nowrap);
	}
	.pending-request-trigger .eyebrow {
		display: var(--host-access-eyebrow-display, inline);
		flex: none;
		color: var(--amber);
		text-transform: uppercase;
		letter-spacing: 0.07em;
		font-size: var(--text-caption);
		font-weight: 650;
	}
	/* The script chip draws §0.1 roles: sentence-case label, status as coloured text. */
	.pending-request-trigger .eyebrow.sentence {
		color: var(--text-tertiary);
		text-transform: none;
		letter-spacing: normal;
		font-size: var(--text-footnote);
		font-weight: var(--fw-semibold);
	}
	.pending-status {
		flex: none;
		color: var(--amber-text);
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
	}
	.pending-status.stale { color: var(--red-text); }
	.pending-status.deciding { color: var(--text-secondary); }
	.pending-action {
		flex: none;
		color: var(--amber);
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
	}
</style>
