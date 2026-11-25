<script lang="ts">
	import { StatePill } from '$lib/components/ui';
	import { duration, owner, pendingLabel } from '$lib/hostAccess/leaseFormat';
	import { fitLabel, fitRow, siteLabels } from '$lib/hostAccess/siteLabel';
	import type { HostAccessItem } from '$lib/api/hostAccess';

	/**
	 * The strip chip that opens a pending request's drawer. A saved-login request is one more request in
	 * the same card: its kind, the agent and the site. It asks for no duration (the person picks one), so it
	 * shows no duration badge; out of date, it shows the same error badge as any request.
	 */
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
	use:fitRow={request.kind === 'fill'}
	{onclick}
>
	{#if request.kind === 'script'}
		<span class="eyebrow sentence">Script approval</span>
		<span class="pending-owner">{request.script_name}</span>
		<span class="pending-status" class:stale class:deciding>{stale ? 'Couldn’t check' : deciding ? 'Saving…' : 'Needs approval'}</span>
	{:else if request.kind === 'fill'}
		<span class="eyebrow">Saved login</span>
		<span class="pending-owner fill">{owner(request)}</span>
		<span class="pending-site" title={request.site} use:fitLabel={siteLabels(request.site, request.domain)}></span>
		{#if stale}<StatePill state="error" label="Couldn’t check" size="sm" />{/if}
	{:else}
		<span class="eyebrow">Temporary host access</span>
		<span class="pending-owner">{owner(request)}</span>
		<StatePill state={stale ? 'error' : 'warning'} label={stale ? 'Couldn’t check' : duration(request.requested_duration_seconds)} size="sm" />
	{/if}
	<span class="pending-action">Review →</span>
</button>

<style>
	.pending-request-trigger {
		/* fitRow sets this on a saved login's row; until it does, the name's 8em fallback applies. */
		--owner-floor: initial;
		display: flex;
		align-items: center;
		gap: var(--space-3);
		width: 100%;
		/* Its row shortens inside the card; the card never widens the stack past the screen. */
		min-width: 0;
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
	/* A saved login's agent gives way first; the site keeps its whole label while it can, then shortens from the front, never below the domain.
	   fitRow lowers the name's 8em floor when the row is short, and puts the site on its own line when even that isn't enough. */
	.pending-owner.fill { flex: 1 1 0; min-width: var(--owner-floor, 8em); white-space: nowrap; }
	.pending-request-trigger:global([data-stacked]) { flex-wrap: wrap; row-gap: var(--space-1); }
	/* fitRow sets data-stacked, so it is not in the markup above. The site's own line: the whole width, after "Review →"; a domain wider than the card wraps rather than being cut. */
	.pending-request-trigger:global([data-stacked]) .pending-site { order: 1; flex: 1 0 100%; min-width: 0; white-space: normal; overflow-wrap: anywhere; }
	.pending-site {
		/* No value until fitLabel measures the labels, so the fallbacks apply until then. */
		--label-full: initial;
		--label-min: initial;
		flex: 0 1 var(--label-full, auto);
		min-width: var(--label-min, 0);
		overflow: hidden;
		color: var(--text-secondary);
		font-size: var(--text-footnote);
		white-space: nowrap;
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
