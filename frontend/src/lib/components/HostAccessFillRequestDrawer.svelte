<script lang="ts">
	import { Button } from '$lib/components/ui';
	import HostAccessBlockedLine from './HostAccessBlockedLine.svelte';
	import HostAccessDrawerHead from './HostAccessDrawerHead.svelte';
	import {
		approvalReady, casReady, duration, fillDurations, fillStartingChoice, owner, UNTIL_REVOKED_LABEL,
	} from '$lib/hostAccess/leaseFormat';
	import type { FillChoice, HostAccessFill } from '$lib/api/hostAccess';

	/**
	 * A pending request to fill a saved login: which agent, which site and username, and for how long.
	 * HostAccessPendingRequestDrawer renders it and owns the shared sheet layout (.pending-drawer,
	 * .decision-*); like the script sheet it draws the §0.1 roles: sentence-case section labels, status
	 * as coloured text, no boxes. The duration is the person's pick, never the agent's: the agent's asked-for
	 * time is only where the choice starts. "Until I revoke it" is the last choice, offered here only (never on
	 * the host-access lease sheet), never where the choice starts, and never reached by wrapping the arrow keys.
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
		request: HostAccessFill;
		stale: boolean;
		deciding: boolean;
		rejectDisabled: boolean;
		approveDisabled: boolean;
		notice?: string | null;
		onApprove: (event: MouseEvent, durationSeconds?: FillChoice) => void;
		onReject: (event: MouseEvent) => void;
		onRetry: () => void;
		onClose: () => void;
	} = $props();

	const ready = $derived(approvalReady(request));
	const STANDING = 'until_revoked' as const;
	const choices = $derived(fillDurations(request.max_duration_seconds, request.requested_duration_seconds));
	const options = $derived<FillChoice[]>(choices.length ? [...choices, STANDING] : []);
	let picked = $state<FillChoice | null>(null);
	const seconds = $derived<FillChoice | null>(
		picked !== null && options.includes(picked) ? picked : fillStartingChoice(choices, request.requested_duration_seconds),
	);

	/**
	 * The arrow keys wrap around the timed choices only: "Until I revoke it" is reached by a deliberate
	 * step forward from the longest one (or a click), never by wrapping back from the first.
	 */
	function choiceKey(event: KeyboardEvent): void {
		const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1
			: event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 0;
		if (!step || seconds === null) return;
		event.preventDefault();
		const at = seconds === STANDING ? choices.length : choices.indexOf(seconds);
		let next: FillChoice;
		if (seconds === STANDING) next = step > 0 ? choices[0] : choices[choices.length - 1];
		else if (step > 0 && at === choices.length - 1) next = STANDING;
		else next = choices[(at + step + choices.length) % choices.length];
		picked = next;
		const group = (event.currentTarget as HTMLElement).closest('[role="radiogroup"]');
		group?.querySelector<HTMLButtonElement>(`[data-seconds="${next}"]`)?.focus();
	}
</script>

<div class="pending-drawer">
	<HostAccessDrawerHead label="Sign in with a saved login" title={owner(request)} status={stale ? 'Couldn’t check' : 'Needs approval'} {stale} {onClose} />
	<div class="decision-body fill-body">
		<p class="explainer">CORSIA types the password into this site for the agent. The agent never sees it, and you can revoke this approval at any time.</p>
		<section><span class="section-label">Site</span><p>{request.site}</p></section>
		<section><span class="section-label">Username</span><p>{request.username}</p></section>
		<section>
			<span class="section-label" id={`fill-duration-${request.id}`}>For how long</span>
			<div class="segmented durations" role="radiogroup" aria-labelledby={`fill-duration-${request.id}`}>
				{#each options as choice (choice)}
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
					>{choice === STANDING ? UNTIL_REVOKED_LABEL : duration(choice)}</button>
				{/each}
			</div>
		</section>
	</div>
	<footer class="decision-actions">
		<HostAccessBlockedLine {stale} incomplete={!ready} kind="fill" rejectable={casReady(request)} {notice} {onRetry} />
		<div class="decision-buttons">
			<span class="decision-spacer"></span>
			<Button variant="secondary" loading={deciding} disabled={rejectDisabled} onclick={onReject}>Reject</Button>
			<Button variant="primary" loading={deciding} disabled={approveDisabled || !ready}
				onclick={(event: MouseEvent) => onApprove(event, seconds ?? undefined)}>Approve</Button>
		</div>
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
	.durations { margin-top: var(--space-2); }
	/* A finger-sized target on any touch pointer; the sheet sets it on a phone-width sheet too. */
	@media (pointer: coarse) {
		.durations .segment { min-height: var(--touch-target); }
	}
</style>
