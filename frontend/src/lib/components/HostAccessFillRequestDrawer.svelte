<script lang="ts">
	import { Button } from '$lib/components/ui';
	import { approvalReady, duration, fillDurations, owner } from '$lib/hostAccess/leaseFormat';
	import type { HostAccessFill } from '$lib/api/hostAccess';

	/**
	 * A pending request to fill a saved login: which agent, which site and username, and for how long.
	 * HostAccessPendingRequestDrawer renders it and owns the shared sheet layout (.pending-drawer,
	 * .decision-*); like the script sheet it draws the §0.1 roles: sentence-case section labels, status
	 * as coloured text, no boxes. The duration is the person's pick, never the agent's.
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
		request: HostAccessFill;
		stale: boolean;
		deciding: boolean;
		rejectDisabled: boolean;
		approveDisabled: boolean;
		onApprove: (event: MouseEvent, durationSeconds?: number) => void;
		onReject: (event: MouseEvent) => void;
		onRetry: () => void;
	} = $props();

	const ready = $derived(approvalReady(request));
	const choices = $derived(fillDurations(request.max_duration_seconds));
	let picked = $state<number | null>(null);
	const seconds = $derived(picked !== null && choices.includes(picked) ? picked : (choices[0] ?? null));

	function choiceKey(event: KeyboardEvent): void {
		const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1
			: event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 0;
		if (!step || seconds === null) return;
		event.preventDefault();
		const next = choices[(choices.indexOf(seconds) + step + choices.length) % choices.length];
		picked = next;
		const group = (event.currentTarget as HTMLElement).closest('[role="radiogroup"]');
		group?.querySelector<HTMLButtonElement>(`[data-seconds="${next}"]`)?.focus();
	}
</script>

<div class="pending-drawer">
	<header class="decision-head">
		<div><div class="section-label">Sign in with a saved login</div><h2 title={owner(request)}>{owner(request)}</h2></div>
		<span class="status" class:stale>{stale ? 'Couldn’t check' : 'Needs approval'}</span>
	</header>
	<div class="decision-body fill-body">
		<p class="explainer">CORSIA types the password into this site for the agent. The agent never sees it, and you can revoke this approval at any time.</p>
		<section><span class="section-label">Site</span><p>{request.site}</p></section>
		<section><span class="section-label">Username</span><p>{request.username}</p></section>
		<section>
			<span class="section-label" id={`fill-duration-${request.id}`}>For how long</span>
			<div class="segmented durations" role="radiogroup" aria-labelledby={`fill-duration-${request.id}`}>
				{#each choices as choice (choice)}
					<button
						type="button"
						class="segment"
						class:active={seconds === choice}
						role="radio"
						data-seconds={choice}
						aria-checked={seconds === choice}
						tabindex={seconds === choice ? 0 : -1}
						onkeydown={choiceKey}
						onclick={() => { picked = choice; }}
					>{duration(choice)}</button>
				{/each}
			</div>
		</section>
		{#if stale || !ready}
			<div class="blocked-line" role="status">
				<span>{stale
					? 'Couldn’t check that this request is still current, so you can’t approve it yet.'
					: 'This request arrived without everything needed to approve it. Retry to load it again.'}</span>
				<Button variant="ghost" size="sm" onclick={onRetry}>Retry</Button>
			</div>
		{/if}
	</div>
	<footer class="decision-actions">
		<span class="decision-spacer"></span>
		<Button variant="ghost" loading={deciding} disabled={rejectDisabled} onclick={onReject}>Reject</Button>
		{#if ready}
			<Button variant="primary" loading={deciding} disabled={approveDisabled}
				onclick={(event: MouseEvent) => onApprove(event, seconds ?? undefined)}>Approve</Button>
		{/if}
	</footer>
</div>

<style>
	.section-label {
		display: block;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		font-weight: var(--fw-semibold);
	}
	/* The last control clears the sticky footer by the same step at every width. */
	.fill-body { padding-bottom: var(--space-4); }
	.explainer { margin: 0; }
	.status {
		flex: none;
		color: var(--amber-text);
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
	}
	.status.stale, .blocked-line { color: var(--red-text); }
	.durations { margin-top: var(--space-2); }
	/* A finger-sized target on a phone-width sheet or any touch pointer. */
	@media (max-width: 720px), (pointer: coarse) {
		.durations .segment { min-height: var(--touch-target); }
	}
	.blocked-line {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-footnote);
	}
	.blocked-line span { flex: 1; min-width: 0; }
</style>
