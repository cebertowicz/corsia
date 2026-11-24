<script lang="ts">
	import { owner, pendingLabel } from '$lib/hostAccess/leaseFormat';
	import { fitLabel, siteLabels } from '$lib/hostAccess/siteLabel';
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
	aria-busy={deciding}
	bind:this={element}
	{onclick}
>
	<span class="pending-kind">{request.kind === 'script' ? 'Script approval' : request.kind === 'fill' ? 'Saved login' : 'Temporary host access'}</span>
	<span class="pending-what">
		<span class="pending-owner">{request.kind === 'script' ? request.script_name : owner(request)}</span>
		{#if request.kind === 'fill'}
			<span class="pending-site" title={request.site} use:fitLabel={siteLabels(request.site, request.domain)}></span>
		{/if}
	</span>
	<span class="pending-status" class:stale class:deciding>{stale ? 'Couldn’t check' : deciding ? 'Saving…' : 'Needs approval'}</span>
	<span class="pending-action">Review →</span>
</button>

<style>
	/* A plain line of text (DESIGN-SYSTEM §0.2): no border, no fill, no pill; the status is the only colour. */
	.pending-request-trigger {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		width: 100%;
		min-height: var(--touch-target);
		padding: var(--space-2) var(--space-3);
		border: none;
		border-radius: var(--radius-sm);
		background: none;
		color: var(--text-primary);
		font: inherit;
		font-size: var(--text-footnote);
		text-align: left;
		cursor: pointer;
		pointer-events: auto;
	}
	.pending-request-trigger:focus-visible {
		outline: 2px solid var(--accent-line);
		outline-offset: 2px;
	}
	.pending-kind {
		display: var(--host-access-eyebrow-display, inline);
		flex: none;
		color: var(--text-tertiary);
		font-weight: var(--fw-semibold);
	}
	.pending-what {
		display: flex;
		flex: 1 1 0;
		align-items: baseline;
		gap: var(--space-2);
		min-width: 0;
	}
	.pending-owner {
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		font-weight: var(--fw-medium);
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	/* One line, shortened in the middle by whole segments (siteLabels), never wrapped. */
	.pending-site {
		flex: 1 1 0;
		min-width: 0;
		overflow: hidden;
		color: var(--text-secondary);
		white-space: nowrap;
	}
	.pending-status {
		flex: none;
		color: var(--amber-text);
		font-weight: var(--fw-medium);
	}
	.pending-status.stale { color: var(--red-text); }
	.pending-status.deciding { color: var(--text-secondary); }
	.pending-action {
		flex: none;
		color: var(--text-primary);
		font-weight: var(--fw-medium);
	}

	/* On a phone the agent and the site stack, so the site keeps a line of its own. */
	@media (max-width: 720px) {
		.pending-what {
			flex-direction: column;
			align-items: stretch;
			gap: 0;
		}
		.pending-site { flex: none; }
	}
</style>
