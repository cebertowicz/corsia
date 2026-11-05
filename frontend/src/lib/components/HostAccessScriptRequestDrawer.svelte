<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { cadence, approvalReady } from '$lib/hostAccess/leaseFormat';
	import type { HostAccessScript } from '$lib/api/hostAccess';

	/**
	 * The pending script request's sheet. HostAccessPendingRequestDrawer renders it and owns the shared
	 * sheet layout (.pending-drawer, .decision-*). It draws the §0.1 roles: sentence-case section labels,
	 * status as coloured text, no boxes.
	 */
	let {
		request,
		stale,
		deciding,
		rejectDisabled,
		approveDisabled,
		onApprove,
		onReject,
		onRetry,
	}: {
		request: HostAccessScript;
		stale: boolean;
		deciding: boolean;
		rejectDisabled: boolean;
		approveDisabled: boolean;
		onApprove: (event: MouseEvent) => void;
		onReject: (event: MouseEvent) => void;
		onRetry: () => void;
	} = $props();

	const ready = $derived(approvalReady(request));
</script>

<div class="pending-drawer">
	<header class="decision-head">
		<div><div class="section-label">Allow this script to act as your agent and use the agents’ GitHub account</div><h2 title={request.script_name}>{request.script_name}</h2></div>
		<span class="status" class:stale>{stale ? 'Not current' : 'Needs approval'}</span>
	</header>
	<div class="decision-body">
		<section><span class="section-label">File</span><p>{request.script_path}</p></section>
		<section><span class="section-label">When it runs</span><p>{cadence(request.trigger?.cadence_seconds ?? null)}</p></section>
		<section class="purpose"><span class="section-label">Purpose</span><p>{request.purpose}</p></section>
		<section><span class="section-label">Request (as the agent quoted it)</span><p>{request.user_request}</p></section>
		<section><span class="section-label">Reviewer’s reason</span><p>{request.reviewer_reason}</p></section>
		{#if stale || !ready}
			<div class="blocked-line" role="status">
				<span>{stale
					? 'Couldn’t check that this request is still current, so it can’t be approved. Retry to check again.'
					: 'This request arrived without everything needed to approve it. Retry to load it again.'}</span>
				<Button variant="ghost" size="sm" onclick={onRetry}>Retry</Button>
			</div>
		{/if}
	</div>
	<footer class="decision-actions">
		<Button variant="danger" loading={deciding} disabled={rejectDisabled} onclick={onReject}>Deny</Button>
		<span class="decision-spacer"></span>
		{#if ready}<Button variant="primary" loading={deciding} disabled={approveDisabled} onclick={onApprove}>Approve</Button>{/if}
	</footer>
</div>

<style>
	.section-label {
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		font-weight: var(--fw-semibold);
	}
	.status {
		flex: none;
		color: var(--amber-text);
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
	}
	.status.stale, .blocked-line { color: var(--red-text); }
	.blocked-line {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-footnote);
	}
	.blocked-line span { flex: 1; min-width: 0; }
</style>
