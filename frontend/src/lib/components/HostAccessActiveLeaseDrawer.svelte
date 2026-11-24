<script lang="ts">
	import { Button } from '$lib/components/ui';
	import HostAccessBlockedLine from './HostAccessBlockedLine.svelte';
	import { duration, owner, remaining } from '$lib/hostAccess/leaseFormat';
	import type { HostAccessTimed } from '$lib/api/hostAccess';

	let {
		lease,
		nowMs,
		disabled,
		deciding,
		notice = null,
		onRevoke,
	}: {
		lease: HostAccessTimed;
		nowMs: number;
		disabled: boolean;
		deciding: boolean;
		/** The server's line when it refused the last Revoke. */
		notice?: string | null;
		onRevoke: (event: MouseEvent) => void;
	} = $props();
</script>

<!-- The same head as the pending request (kind label over the agent's name) and the same §0.1
     rows: a section label over plain text. -->
<div class="active-drawer">
	<header class="active-drawer-head">
		<div class="section-label">{lease.kind === 'fill' ? 'Sign in with a saved login' : 'Temporary host access'}</div>
		<h2 title={owner(lease)}>{owner(lease)}</h2>
	</header>
	<div class="active-drawer-body">
		{#if lease.kind === 'fill'}
			<p>{owner(lease)} can sign in to {lease.site} as {lease.username}. CORSIA types the password; the agent never sees it.</p>
			<dl>
				<div class="wide"><dt class="section-label">Site</dt><dd>{lease.site}</dd></div>
				<div class="wide"><dt class="section-label">Username</dt><dd>{lease.username}</dd></div>
				<div><dt class="section-label">For how long</dt><dd>{duration(lease.duration_seconds)}</dd></div>
				<div><dt class="section-label">Time remaining</dt><dd>{remaining(lease, nowMs)}</dd></div>
			</dl>
		{:else}
			<p>{lease.purpose}</p>
			<dl>
				<div><dt class="section-label">For how long</dt><dd>{duration(lease.requested_duration_seconds)}</dd></div>
				<div><dt class="section-label">Reviewer</dt><dd>{lease.reviewer?.display_name ?? lease.reviewer?.model ?? 'Unavailable'}</dd></div>
				<div><dt class="section-label">Approval</dt><dd>{lease.application_review_result?.approval_mode === 'automatic' ? 'Automatic reviewer approval' : 'User approval'}</dd></div>
				<div><dt class="section-label">Time remaining</dt><dd>{remaining(lease, nowMs)}</dd></div>
			</dl>
		{/if}
	</div>
	<div class="active-drawer-actions">
		<HostAccessBlockedLine {notice} />
		<Button
			variant="ghost"
			size="sm"
			loading={deciding}
			{disabled}
			onclick={onRevoke}
		>
			{lease.kind === 'fill' ? 'Revoke approval' : 'Revoke host access'}
		</Button>
	</div>
</div>

<style>
	/* Sticky within AdaptiveDrawer's own .drawer-body scroll, not a second
	   nested scroller — the same technique DecisionChip already uses for its
	   header in this exact shared drawer. A percentage/flex height chain
	   through .drawer-body's max-height-capped flex item does not reliably
	   resolve (verified empirically), so sticky is the robust fix. */
	.active-drawer {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	/* Opaque strips in the sheet's own fill, as the pending sheet's. */
	.active-drawer-head {
		position: sticky;
		top: 0;
		z-index: 2;
		padding: var(--space-2) 0 var(--space-3);
		border-bottom: 1px solid var(--border-subtle);
		background: var(--bg-secondary);
	}
	.active-drawer-head h2 {
		margin: var(--space-1) 0 0;
		overflow: hidden;
		color: var(--text-primary);
		font-size: var(--text-title3);
		letter-spacing: -0.02em;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.section-label {
		display: block;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		font-weight: var(--fw-semibold);
	}
	/* The last row clears the sticky footer by the same step as the pending sheets. */
	.active-drawer-body {
		display: grid;
		gap: var(--space-3);
		padding-bottom: var(--space-4);
	}
	.active-drawer-body p {
		margin: 0;
		color: var(--text-secondary);
		line-height: 1.4;
		overflow-wrap: anywhere;
	}
	.active-drawer-body dl {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-3);
		margin: 0;
	}
	.active-drawer-body dl > div { min-width: 0; }
	/* A site or username can be long: each takes the full row, as on the request sheet. */
	.active-drawer-body dl > .wide { grid-column: 1 / -1; }
	.active-drawer-body dd {
		min-width: 0;
		margin: var(--space-1) 0 0;
		color: var(--text-secondary);
		overflow-wrap: anywhere;
	}
	.active-drawer-actions {
		position: sticky;
		bottom: 0;
		z-index: 2;
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: var(--space-2);
		/* Flush-bottom sheet: the footer carries the bottom inset itself. */
		padding: var(--space-3) 0 max(var(--space-4), env(safe-area-inset-bottom, 0px));
		border-top: 1px solid var(--border-subtle);
		background: var(--bg-secondary);
	}
	.active-drawer-actions :global(.blocked-line) { align-self: stretch; }
	/* The ghost action's word ends where a request sheet's Approve ends: pull it out of its own padding. */
	.active-drawer-actions > :global(.btn.ghost) { margin-right: calc(-1 * (var(--button-pad-x-sm) + 1px)); }
</style>
