<script lang="ts">
	import { Button } from '$lib/components/ui';
	import HostAccessBlockedLine from './HostAccessBlockedLine.svelte';
	import HostAccessDrawerHead from './HostAccessDrawerHead.svelte';
	import { cadence, approvalReady, casReady, text } from '$lib/hostAccess/leaseFormat';
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
		notice = null,
		onApprove,
		onReject,
		onRetry,
		onClose,
	}: {
		request: HostAccessScript;
		stale: boolean;
		deciding: boolean;
		rejectDisabled: boolean;
		approveDisabled: boolean;
		notice?: string | null;
		onApprove: (event: MouseEvent) => void;
		onReject: (event: MouseEvent) => void;
		onRetry: () => void;
		onClose: () => void;
	} = $props();

	const ready = $derived(approvalReady(request));
</script>

<div class="pending-drawer">
	<HostAccessDrawerHead label="Allow this script to act as your agent and use the agents’ GitHub account" title={request.script_name} status={stale ? 'Couldn’t check' : 'Needs approval'} {stale} {onClose} />
	<div class="decision-body">
		<section><span class="section-label">File</span><p>{request.script_path}</p></section>
		<section><span class="section-label">When it runs</span><p>{cadence(request.trigger?.cadence_seconds ?? null)}</p></section>
		{#if text(request.purpose)}
			<section class="purpose"><span class="section-label">Purpose</span><p>{request.purpose}</p></section>
		{/if}
		<section><span class="section-label">Request (as the agent quoted it)</span><p>{request.user_request}</p></section>
		<section><span class="section-label">Reviewer’s reason</span><p>{request.reviewer_reason}</p></section>
	</div>
	<footer class="decision-actions">
		<HostAccessBlockedLine {stale} incomplete={!ready} kind="script" rejectable={casReady(request)} {notice} {onRetry} />
		<div class="decision-buttons">
			<span class="decision-spacer"></span>
			<Button variant="secondary" loading={deciding} disabled={rejectDisabled} onclick={onReject}>Deny</Button>
			<Button variant="primary" loading={deciding} disabled={approveDisabled || !ready} onclick={onApprove}>Approve</Button>
		</div>
	</footer>
</div>

<style>
	.section-label {
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		font-weight: var(--fw-semibold);
	}
</style>
