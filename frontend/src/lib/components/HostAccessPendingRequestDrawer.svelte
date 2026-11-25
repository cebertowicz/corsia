<script lang="ts">
	import { Button } from '$lib/components/ui';
	import HostAccessScriptRequestDrawer from './HostAccessScriptRequestDrawer.svelte';
	import HostAccessFillRequestDrawer from './HostAccessFillRequestDrawer.svelte';
	import HostAccessBlockedLine from './HostAccessBlockedLine.svelte';
	import HostAccessDrawerHead from './HostAccessDrawerHead.svelte';
	import { text, duration, policyLabel, approvalReady, casReady, owner } from '$lib/hostAccess/leaseFormat';
	import type { FillChoice, HostAccessItem } from '$lib/api/hostAccess';
	let {
		lease,
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
		lease: HostAccessItem;
		stale: boolean;
		/** Whether this lease's own decision is in flight, vs. another lease's. */
		deciding: boolean;
		/** Mirrors the parent decide() guard so a visibly-enabled control never silently no-ops. */
		rejectDisabled: boolean;
		approveDisabled: boolean;
		/** The server's line when it refused this request's last decision. */
		notice?: string | null;
		/** A fill approval passes the duration the person picked; nothing else does. */
		onApprove: (event: MouseEvent, durationSeconds?: FillChoice) => void;
		onReject: (event: MouseEvent) => void;
		onRetry: () => void;
		/** Done, on the sheet's own head row. */
		onClose: () => void;
	} = $props();

	const ready = $derived(approvalReady(lease));
	const exceptions = $derived(lease.kind !== 'script' && lease.kind !== 'fill' && Array.isArray(lease.requested_policy_exceptions)
		? lease.requested_policy_exceptions : []);
	const external = $derived(exceptions.some((exception) => exception?.policy === 'external_agent'));
</script>

{#snippet policyExceptions()}
	<section class="policy-exceptions" aria-label="Beyond the usual rules">
		<span class="label exceptions-label">Beyond the usual rules</span>
		{#if external}
			<p class="external-warning">This agent runs outside CORSIA’s controls, so CORSIA may not see what it does or what it costs. You approve this yourself, even when Auto-approve is on.</p>
		{/if}
		<ul>
			{#each exceptions as exception}
				<li>
					{#if exception.policy === 'external_agent'}
						<strong>External agent</strong>
						<p>{exception.reason}</p>
						<div class="label launch-label">Command</div>
						{#if text(exception.command)}
							<code>{exception.command}</code>
						{:else}
							<p class="launch-missing">Command missing</p>
						{/if}
						<div class="label launch-label">Working folder</div>
						{#if text(exception.working_directory)}
							<code>{exception.working_directory}</code>
						{:else}
							<p class="launch-missing">Working folder missing</p>
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
	<p class="explainer">This agent is asking for temporary access to parts of this computer it can’t normally reach. It cannot use that access unless you approve, and you can revoke it at any time.</p>
{/snippet}

{#if lease.kind === 'script'}
	<HostAccessScriptRequestDrawer request={lease} {stale} {deciding} {rejectDisabled} {approveDisabled} {notice} {onApprove} {onReject} {onRetry} {onClose} />
{:else if lease.kind === 'fill'}
	<HostAccessFillRequestDrawer request={lease} {stale} {deciding} {rejectDisabled} {approveDisabled} {notice} {onApprove} {onReject} {onRetry} {onClose} />
{:else}
<div class="pending-drawer">
	<HostAccessDrawerHead label="Temporary host access" title={owner(lease)} status={stale ? 'Couldn’t check' : 'Needs approval'} {stale} {onClose} />

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
			<section><span class="label">For how long</span><p>{duration(lease.requested_duration_seconds)}</p></section>
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

	</div>

	<footer class="decision-actions">
		<HostAccessBlockedLine {stale} incomplete={!ready} kind="lease" rejectable={casReady(lease)} {notice} {onRetry} />
		<!-- Approve is always there, disabled when it can't be used, so Reject never moves. -->
		<div class="decision-buttons">
			<span class="decision-spacer"></span>
			<Button variant="secondary" loading={deciding} disabled={rejectDisabled} onclick={onReject}>Reject</Button>
			<Button variant="primary" loading={deciding} disabled={approveDisabled || !ready} onclick={onApprove}>Approve</Button>
		</div>
	</footer>
</div>
{/if}

<style>
	/* The sheet both request kinds share (HostAccessScriptRequestDrawer renders inside it too),
	   so it is global under .pending-drawer. */
	:global(.pending-drawer) {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	/* The head (HostAccessDrawerHead) and the footer are sticky within AdaptiveDrawer's own .drawer-body
	   scroll, not a second nested scroller: a flex height chain through .drawer-body's max-height-capped
	   flex item does not reliably resolve, so sticky is the robust fix. */
	:global(.pending-drawer .decision-body) {
		display: grid;
		gap: 18px;
		min-width: 0;
	}
	:global(.pending-drawer .decision-body section) { min-width: 0; }
	:global(.pending-drawer .decision-body p) {
		margin: 5px 0 0;
		color: var(--text-secondary);
		line-height: 1.5;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	:global(.pending-drawer .purpose p) { font-size: var(--text-body); }
	/* The footer holds why Approve is off (when it is) above the decision, so a disabled or
	   missing Approve always shows its reason at every width without scrolling. */
	:global(.pending-drawer .decision-actions) {
		position: sticky;
		bottom: 0;
		z-index: 2;
		display: grid;
		gap: var(--space-2);
		/* The sheet is flush-bottom, so the footer carries the bottom inset itself
		   and sits on the scroller's edge instead of above a strip of padding the
		   content scrolls through. */
		padding: var(--space-3) 0 max(var(--space-4), env(safe-area-inset-bottom, 0px));
		/* A hairline over the pinned footer, as the decision drawer draws it, so content scrolling
		   under it ends at a rule instead of a hard cut. */
		border-top: 1px solid var(--border-subtle);
		background: var(--bg-secondary);
		/* Its own paint layer: without it Chromium leaves the footer's lowest strip unpainted when a tall
		   footer (the incomplete request's line) is pinned near the top of the scroll, and the body text
		   scrolls visibly through it. Unlike paint containment, this does not clip the buttons' focus rings. */
		will-change: transform;
	}
	/* Reject sits beside Approve, a button's gap apart; on a phone a finger's width, so one is never
	   pressed for the other. */
	:global(.pending-drawer .decision-buttons) {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}
	:global(.pending-drawer .decision-spacer) { flex: 1; }

	.label {
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		font-weight: var(--fw-semibold);
	}
	.decision-body .explainer { margin: 0; }
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
	/* Every section is a label over plain text: no card border or fill. */
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
	/* No box, but the label keeps its warning colour: it is the drawer's one risk signal. */
	.policy-exceptions .exceptions-label { color: var(--amber-text); }
	.policy-exceptions .external-warning {
		color: var(--text-primary);
		margin: 8px 0 16px;
	}
	.launch-label { margin-top: 9px; }
	/* The exact launch reads as a text field's value: its outline, no fill. */
	.policy-exceptions code {
		display: block;
		margin-top: 4px;
		padding: 8px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: none;
		font-family: var(--font-mono);
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
	/* The last line clears the sticky footer by the same step as the fill drawer's. */
	.decision-body { padding-bottom: var(--space-4); }

	@media (max-width: 720px) {
		/* Keep the first requested exception above the sticky decision footer even
		   when a non-Apple system font takes an extra line. The drawer remains the
		   only scroll container; this only compacts existing sheet spacing. */
		/* A segmented choice in a request sheet (the fill's duration) is finger-sized on a phone. */
		:global(.pending-drawer .segmented .segment) { min-height: var(--touch-target); }
		:global(.pending-drawer .decision-body) { gap: var(--space-3); }
		:global(.pending-drawer .decision-actions) { padding-top: var(--space-2); }
		:global(.pending-drawer .decision-buttons) { gap: var(--touch-target); }
		.policy-exceptions .external-warning {
			margin: var(--space-2) 0;
			line-height: var(--lh-snug);
		}
		.repo-policy-context li {
			grid-template-columns: 1fr;
			gap: 2px;
		}
		.risk-grid { grid-template-columns: 1fr; }
	}
</style>
