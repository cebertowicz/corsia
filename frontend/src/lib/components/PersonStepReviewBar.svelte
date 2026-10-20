<script lang="ts">
	import { get } from 'svelte/store';
	import { Avatar, Button } from '$lib/components/ui';
	import MiddleName from './MiddleName.svelte';
	import { agentColor } from '$lib/docPresence';
	import { token as authToken } from '$lib/stores/auth';
	import { mutateUserTask } from '$lib/api/plans';
	import type { PersonStepInstruction } from '$lib/api/reviewBar';

	type Receipt = 'done' | 'wont_do';
	let {
		personStep,
		receipt = $bindable<Receipt | null>(null),
		onAnswered,
		onConflict,
	}: {
		personStep: PersonStepInstruction | null;
		receipt?: Receipt | null;
		onAnswered?: () => void;
		onConflict?: () => void;
	} = $props();

	let action = $state<'idle' | 'sending'>('idle');
	let failure = $state('');
	const busy = $derived(action === 'sending');

	async function settle(operation: 'mark_done' | 'close_node') {
		const step = personStep;
		const bearer = get(authToken);
		if (!step || !bearer || busy) return;
		action = 'sending';
		failure = '';
		const outcome = await mutateUserTask(
			bearer, step.plan_id, operation, step.node_id, step.node_revision,
			`${operation}:${step.node_id}:${step.node_revision}`,
		);
		if (outcome.status === 'success') {
			receipt = operation === 'mark_done' ? 'done' : 'wont_do';
			onAnswered?.();
			return;
		}
		action = 'idle';
		if (outcome.status === 'conflict') {
			failure = outcome.detail ?? 'This step changed. Refreshing the latest state.';
			onConflict?.();
			return;
		}
		failure = outcome.status === 'forbidden'
			? 'You no longer have permission to change this step.'
			: outcome.message;
	}
</script>

{#if receipt}
	<section class="review-bar receipt" aria-label="Person step answered">
		<svg class="rb-icon" aria-hidden="true" viewBox="0 0 16 16">
			{#if receipt === 'done'}<circle cx="8" cy="8" r="6" /><path d="M5.3 8.2l1.9 1.9 3.5-3.9" />
			{:else}<circle cx="8" cy="8" r="6" /><path d="M5 8h6" />{/if}
		</svg>
		<span class="rb-receipt" role="status" tabindex="-1">{receipt === 'done' ? 'Done' : 'Won’t do'}</span>
	</section>
{:else if personStep}
	<section class="review-bar ask" aria-label="Person step" aria-busy={busy}>
		<span class="rb-head">
			<span class="rb-line">
				<span class="rb-lead"><Avatar name={personStep.owner_name} color={agentColor(personStep.owner_thread_id)} hidden /></span>
				<span class="rb-who"><strong><MiddleName name={personStep.owner_name} /></strong></span>
			</span>
			<p class="rb-ask-full">needs you to do this: {personStep.node_title}</p>
		</span>
		<span class="rb-row2"><div class="rb-capsule">
			<span class="rb-status" class:busy role="status">{busy ? 'Recording your answer…' : failure}</span>
			<div class="rb-actions">
				<Button size="sm" variant="ghost" disabled={busy} onclick={() => void settle('close_node')}>Won’t do</Button>
				<Button size="sm" variant="primary" loading={busy} onclick={() => void settle('mark_done')}>Done</Button>
			</div>
		</div></span>
	</section>
{/if}

<style>
	.review-bar { flex: none; display: flex; align-items: center; gap: var(--space-2); min-height: var(--space-7); padding: 0 var(--space-1) 0 0; font-size: var(--text-footnote); color: var(--text-secondary); min-width: 0; }
	.review-bar.receipt { min-height: calc(var(--space-7) * 2); }
	.review-bar.ask { flex-direction: column; align-items: stretch; gap: 0; border: 0; border-radius: 0; background: transparent; box-shadow: none; }
	.rb-head { display: flex; flex-direction: column; min-width: 0; }
	.rb-line { display: flex; align-items: center; gap: var(--space-2); min-height: var(--space-7); min-width: 0; padding: 0; color: var(--text-primary); }
	.rb-lead { display: contents; }
	.rb-who { flex: 0 1 auto; display: flex; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.rb-who strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: var(--fw-semibold); }
	.rb-ask-full { margin: 0; padding: 0 0 var(--space-1); color: var(--text-secondary); line-height: var(--lh-normal); overflow-wrap: anywhere; }
	.rb-row2 { display: flex; align-items: center; gap: var(--space-2); min-width: 0; min-height: var(--space-7); }
	.rb-capsule { display: contents; }
	.rb-status { flex: 1 1 0; min-width: 0; color: var(--amber-text); }
	.rb-status.busy { color: var(--text-secondary); }
	.rb-actions { display: inline-flex; align-items: center; gap: var(--space-1); flex: none; margin-left: auto; }
	.rb-icon { width: var(--space-4); height: var(--space-4); flex: none; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; color: var(--green); }
	.rb-receipt { flex: 1 1 0; min-width: 0; outline: none; color: var(--text-primary); font-weight: var(--fw-medium); }
	:global(.document-header) .review-bar.ask { grid-column: 1 / -1; display: grid; grid-template-columns: subgrid; }
	:global(.document-header) .review-bar.ask .rb-head { display: contents; }
	/* As the review ask: who asks shares the header's first row with its icons; the ask and actions span it. */
	:global(.document-header) .review-bar.ask .rb-line { grid-column: 1; }
	:global(.document-header) .review-bar.ask .rb-ask-full, :global(.document-header) .review-bar.ask .rb-row2 { grid-column: 1 / -1; }
</style>
