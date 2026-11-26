<script lang="ts">
	import { Button, Field } from '$lib/components/ui';
	import HostAccessBlockedLine from './HostAccessBlockedLine.svelte';
	import HostAccessDrawerHead from './HostAccessDrawerHead.svelte';
	import {
		approvalReady, casReady, duration, fillDurations, fillStartingChoice, owner, UNTIL_TURNED_OFF_LABEL,
	} from '$lib/hostAccess/leaseFormat';
	import type { FillChoice, HostAccessFill } from '$lib/api/hostAccess';

	/**
	 * A pending request to fill a saved login or a saved item: which agent, which site, which username (a login)
	 * or item (by its label), and for how long. Only the head label, the first line and that row differ.
	 * HostAccessPendingRequestDrawer renders it and owns the shared sheet layout (.pending-drawer,
	 * .decision-*); like the script sheet it draws the §0.1 roles: sentence-case section labels, status
	 * as coloured text, no boxes. The duration is the person's pick, never the agent's: the agent's asked-for
	 * time is only where the choice starts. The choice is one select (DESIGN-SYSTEM §3.4, Field `nested`), in time
	 * order, with "Until I turn it off" last: offered here only (never on the host-access lease sheet) and never
	 * where the choice starts. The sheet says nothing about revoking: no screen ends a standing grant yet, so the
	 * line would be untrue under "Until I turn it off"; a timed grant's chip carries its own revoke.
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

	const fieldId = $derived(`fill-duration-${request.id}`);

	function pick(event: Event): void {
		const value = (event.currentTarget as HTMLSelectElement).value;
		picked = options.find((choice) => String(choice) === value) ?? null;
	}
</script>

<div class="pending-drawer">
	<HostAccessDrawerHead label={request.item_label ? 'Use a saved item' : 'Sign in with a saved login'} title={owner(request)} status={stale ? 'Couldn’t check' : 'Needs approval'} {stale} {onClose} />
	<div class="decision-body fill-body">
		<p class="explainer">{request.item_label ? 'CORSIA fills it in on this site for the agent. The agent never sees it.' : 'CORSIA fills in the password on this site for the agent. The agent never sees it.'}</p>
		<section><span class="section-label">Site</span><p>{request.site}</p></section>
		{#if request.item_label}
			<section><span class="section-label">Saved item</span><p>{request.item_label}</p></section>
		{:else}
			<section><span class="section-label">Username</span><p>{request.username}</p></section>
		{/if}
		{#if options.length}
			<section>
				<!-- The sheet's own section label, like Site and Username; Field keeps the select's accessible name. -->
				<span class="section-label" aria-hidden="true">Duration</span>
				<div class="duration">
					<Field variant="nested" layout="stacked" hideLabel label="Duration" labelFor={fieldId}>
						<select id={fieldId} value={seconds === null ? '' : String(seconds)} onchange={pick}>
							{#each options as choice (choice)}
								<option value={String(choice)}>{choice === STANDING ? UNTIL_TURNED_OFF_LABEL : duration(choice)}</option>
							{/each}
						</select>
					</Field>
				</div>
			</section>
		{/if}
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
	.duration { margin-top: var(--space-2); }
</style>
