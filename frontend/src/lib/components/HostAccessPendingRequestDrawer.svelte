<script lang="ts">
	import { Button, StatePill } from '$lib/components/ui';
	import { text, duration, policyLabel, approvalReady, owner } from '$lib/hostAccess/leaseFormat';
	import type { HostAccessLease } from '$lib/api/hostAccess';

	let {
		lease,
		stale,
		deciding,
		rejectDisabled,
		approveDisabled,
		onApprove,
		onReject,
		onRetry,
	}: {
		lease: HostAccessLease;
		stale: boolean;
		/** Whether this lease's own decision is in flight, vs. another lease's. */
		deciding: boolean;
		/** Mirrors the parent decide() guard so a visibly-enabled control never silently no-ops. */
		rejectDisabled: boolean;
		approveDisabled: boolean;
		onApprove: (event: MouseEvent) => void;
		onReject: (event: MouseEvent) => void;
		onRetry: () => void;
	} = $props();

	const ready = $derived(approvalReady(lease));
	const exceptions = $derived(Array.isArray(lease.requested_policy_exceptions)
		? lease.requested_policy_exceptions : []);
	const external = $derived(exceptions.some((exception) => exception?.policy === 'external_agent'));
</script>

{#snippet policyExceptions()}
	<section class="policy-exceptions" aria-label="Policy exceptions requested">
		<span class="label">Policy exceptions requested</span>
		{#if external}
			<p class="external-warning">External agents run outside CORSIA’s managed context and controls. Their instructions, skills and tools may differ, and CORSIA may not fully observe their actions, costs or results. Human approval is required for this exception, even when automatic approval is enabled.</p>
		{/if}
		<ul>
			{#each exceptions as exception}
				<li>
					{#if exception.policy === 'external_agent'}
						<strong>External agent</strong>
						<p>{exception.reason}</p>
						<div class="launch-label">Command</div>
						{#if text(exception.command)}
							<code>{exception.command}</code>
						{:else}
							<p class="launch-missing">Command missing</p>
						{/if}
						<div class="launch-label">Working directory</div>
						{#if text(exception.working_directory)}
							<code>{exception.working_directory}</code>
						{:else}
							<p class="launch-missing">Working directory missing</p>
						{/if}
					{:else}
						<strong>{exception.repo_name}: {policyLabel(exception.policy)}</strong>
						<p>{exception.reason}</p>
					{/if}
				</li>
			{/each}
		</ul>
	</section>
{/snippet}

<!-- A first request can arrive before anything else has explained host access. -->
{#snippet explainer()}
	<p class="explainer">This agent is asking for temporary access to parts of this computer it can't normally reach. It cannot use that access unless you approve, and you can revoke it at any time.</p>
{/snippet}

<div class="pending-drawer">
	<header class="decision-head">
		<div>
			<div class="eyebrow">Temporary host access</div>
			<h2 title={owner(lease)}>{owner(lease)}</h2>
		</div>
		<StatePill state={stale ? 'error' : 'warning'} label={stale ? 'State stale' : duration(lease.requested_duration_seconds)} />
	</header>

	<div class="decision-body">
		{#if !external}
			{@render explainer()}
		{/if}

		<section class="purpose">
			<span class="label">Purpose</span>
			<p>{text(lease.purpose) ? lease.purpose : 'Unavailable'}</p>
		</section>

		{#if external}
			<!-- The external warning and exact launch must stay above the footer on a phone. -->
			{@render policyExceptions()}
			{@render explainer()}
		{/if}

		<section class="plan">
			<span class="label">Plan</span>
			{#if Array.isArray(lease.plan) && lease.plan.length && lease.plan.every(text)}
				<ol>
					{#each lease.plan as step}
						<li>{step}</li>
					{/each}
				</ol>
			{:else}
				<p>Unavailable</p>
			{/if}
		</section>

		<div class="risk-grid">
			{#if text(lease.expected_resources)}
				<section><span class="label">Resources</span><p>{lease.expected_resources}</p></section>
			{/if}
			{#if text(lease.persistent_effects)}
				<section><span class="label">Persistent effects</span><p>{lease.persistent_effects}</p></section>
			{/if}
			{#if text(lease.rollback_plan)}
				<section><span class="label">Rollback</span><p>{lease.rollback_plan}</p></section>
			{/if}
			{#if text(lease.forbidden_effects)}
				<section><span class="label">Out of bounds</span><p>{lease.forbidden_effects}</p></section>
			{/if}
		</div>

		<section class="repo-policy-context">
			<span class="label">Repository policy context</span>
			{#if text(lease.requesting_project_id)}
				<p class="project-id">Project: {lease.requesting_project_id}</p>
				{#if Array.isArray(lease.repo_policy_snapshot) && lease.repo_policy_snapshot.length}
					<ul>
						{#each lease.repo_policy_snapshot as repo}
							<li>
								<strong>{repo.repo_name}</strong>
								<span>{text(repo.pinned_branch) ? `Pinned to ${repo.pinned_branch}` : 'Branch unpinned'}</span>
								<span>{repo.protect_base_checkout ? 'Base checkout protected' : 'Base checkout unprotected'}</span>
							</li>
						{/each}
					</ul>
				{:else}
					<p>No registered repository policy.</p>
				{/if}
			{:else}
				<p>No project repository policy attached.</p>
			{/if}
		</section>

		{#if !external && exceptions.length}
			{@render policyExceptions()}
		{/if}

		<section class="review">
			<span class="label">Independent review</span>
			<strong>{text(lease.reviewer?.display_name)
				? lease.reviewer.display_name
				: (text(lease.reviewer?.model) ? lease.reviewer.model : 'Unavailable')}</strong>
			{#if text(lease.reviewer?.attestation)}
				<p>{lease.reviewer.attestation}</p>
			{/if}
		</section>

		{#if stale || !ready}
			<div class="approval-blocked" role="status">
				<strong>Approval is blocked.</strong>
				<span>{stale
					? 'Refresh authoritative state before deciding.'
					: 'The request, independent attestation, or decision identity is incomplete or malformed.'}</span>
				<Button size="sm" onclick={onRetry}>Retry</Button>
			</div>
		{/if}
	</div>

	<footer class="decision-actions">
		<Button
			variant="danger"
			loading={deciding}
			disabled={rejectDisabled}
			onclick={onReject}
		>
			Reject
		</Button>
		<span class="decision-spacer"></span>
		{#if ready}
			<Button
				variant="primary"
				loading={deciding}
				disabled={approveDisabled}
				onclick={onApprove}
			>
				Approve
			</Button>
		{/if}
	</footer>
</div>

<style>
	.pending-drawer {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	/* Sticky within AdaptiveDrawer's own .drawer-body scroll, not a second
	   nested scroller — the same technique DecisionChip already uses for its
	   header in this exact shared drawer. A percentage/flex height chain
	   through .drawer-body's max-height-capped flex item does not reliably
	   resolve (verified empirically), so sticky is the robust fix, not a
	   nested `.decision-body { overflow-y: auto }` region. */
	.decision-head {
		position: sticky;
		top: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 18px;
		padding: 8px 0 14px;
		background: var(--glass-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
	}
	.decision-head > div { min-width: 0; }
	.decision-head h2 {
		margin: 4px 0 0;
		overflow: hidden;
		color: var(--text-primary);
		font-size: var(--text-title3);
		letter-spacing: -0.02em;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.eyebrow, .label {
		color: var(--text-tertiary);
		text-transform: uppercase;
		letter-spacing: 0.07em;
		font-size: var(--text-caption);
		font-weight: 650;
	}
	.decision-body {
		display: grid;
		gap: 18px;
		min-width: 0;
	}
	.decision-body section { min-width: 0; }
	.decision-body p {
		margin: 5px 0 0;
		color: var(--text-secondary);
		line-height: 1.5;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.decision-body .explainer { margin: 0; }
	.purpose p { color: var(--text-primary); font-size: var(--text-body); }
	ol {
		margin: 7px 0 0;
		padding-left: 22px;
		display: grid;
		gap: 7px;
		color: var(--text-secondary);
		line-height: 1.45;
	}
	.risk-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 12px;
	}
	.risk-grid section, .review {
		padding: 12px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		background: var(--bg-secondary);
	}
	.repo-policy-context {
		padding: 12px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		background: var(--bg-secondary);
	}
	.repo-policy-context ul, .policy-exceptions ul {
		display: grid;
		gap: 8px;
		margin: 8px 0 0;
		padding: 0;
		list-style: none;
	}
	.repo-policy-context li {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		gap: 8px;
		color: var(--text-secondary);
		font-size: var(--text-footnote);
	}
	.repo-policy-context li strong { color: var(--text-primary); }
	.project-id { font-size: var(--text-footnote); }
	.policy-exceptions {
		padding: 12px;
		border: 1px solid color-mix(in srgb, var(--amber) 45%, var(--border));
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--amber) 8%, var(--bg-secondary));
	}
	.policy-exceptions .label { color: var(--amber); }
	.policy-exceptions .external-warning {
		color: var(--text-primary);
		margin: 8px 0 16px;
	}
	.launch-label { margin-top: 9px; font-size: var(--text-footnote); color: var(--text-tertiary); }
	.policy-exceptions code {
		display: block;
		margin-top: 4px;
		padding: 8px;
		border-radius: var(--radius-sm);
		background: var(--bg-secondary);
		font-size: var(--text-footnote);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.policy-exceptions .launch-missing { color: var(--text-secondary); }
	.policy-exceptions li strong {
		display: block;
		color: var(--text-primary);
	}
	.review strong {
		display: block;
		margin-top: 6px;
		color: var(--text-primary);
		font-size: var(--text-subhead);
	}
	.approval-blocked {
		display: grid;
		grid-template-columns: auto 1fr auto;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		border: 1px solid color-mix(in srgb, var(--red) 30%, transparent);
		border-radius: var(--radius-sm);
		background: var(--red-bg);
		color: var(--red);
		font-size: var(--text-footnote);
	}
	.approval-blocked span { color: var(--text-secondary); }
	.decision-actions {
		position: sticky;
		bottom: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 14px 0 4px;
		background: var(--glass-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
	}
	.decision-spacer { flex: 1; }

	@media (max-width: 720px) {
		/* Keep the first requested exception above the sticky decision footer even
		   when a non-Apple system font takes an extra line. The drawer remains the
		   only scroll container; this only compacts existing sheet spacing. */
		.decision-head { padding-bottom: var(--space-2); }
		.decision-body { gap: var(--space-3); }
		.policy-exceptions { padding: var(--space-2); }
		.policy-exceptions .external-warning {
			margin: var(--space-2) 0;
			line-height: var(--lh-snug);
		}
		.decision-actions { padding-top: var(--space-2); }
		.repo-policy-context li {
			grid-template-columns: 1fr;
			gap: 2px;
		}
		.risk-grid { grid-template-columns: 1fr; }
		.approval-blocked {
			grid-template-columns: 1fr auto;
		}
		.approval-blocked strong { grid-column: 1 / -1; }
	}
</style>
