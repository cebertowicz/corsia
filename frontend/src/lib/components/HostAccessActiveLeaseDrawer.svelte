<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { duration, remaining } from '$lib/hostAccess/leaseFormat';
	import type { HostAccessLease } from '$lib/api/hostAccess';

	let {
		lease,
		nowMs,
		disabled,
		deciding,
		onRevoke,
	}: {
		lease: HostAccessLease;
		nowMs: number;
		disabled: boolean;
		deciding: boolean;
		onRevoke: (event: MouseEvent) => void;
	} = $props();
</script>

<div class="active-drawer">
	<div class="active-drawer-body">
		<p>{lease.purpose}</p>
		<dl>
			<dt>Approved duration</dt>
			<dd>{duration(lease.requested_duration_seconds)}</dd>
			<dt>Reviewer</dt>
			<dd>{lease.reviewer?.display_name ?? lease.reviewer?.model ?? 'Unavailable'}</dd>
			<dt>Approval</dt>
			<dd>{lease.application_review_result?.approval_mode === 'automatic' ? 'Automatic reviewer approval' : 'User approval'}</dd>
			<dt>Time remaining</dt>
			<dd>{remaining(lease, nowMs)}</dd>
		</dl>
	</div>
	<div class="active-drawer-actions">
		<Button
			variant="danger"
			size="sm"
			loading={deciding}
			{disabled}
			onclick={onRevoke}
		>
			Revoke host access
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
	.active-drawer-body {
		display: grid;
		gap: var(--space-3);
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
		gap: var(--space-1) var(--space-3);
		margin: 0;
	}
	.active-drawer-body dt { color: var(--text-tertiary); }
	.active-drawer-body dd {
		min-width: 0;
		margin: 0;
		color: var(--text-primary);
		text-align: right;
		overflow-wrap: anywhere;
	}
	.active-drawer-actions {
		position: sticky;
		bottom: 0;
		z-index: 2;
		display: flex;
		justify-content: flex-end;
		padding-top: var(--space-3);
		background: var(--glass-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
	}

	@media (max-width: 720px) {
		.active-drawer-body dl { grid-template-columns: 1fr 1fr; }
	}
</style>
