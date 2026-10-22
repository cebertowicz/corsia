<script lang="ts">
	import { get } from 'svelte/store';
	import { Button } from '$lib/components/ui';
	import ReviewBarShell from './ReviewBarShell.svelte';
	import { token as authToken } from '$lib/stores/auth';
	import { mutateUserTask } from '$lib/api/plans';
	import type { PersonStepInstruction } from '$lib/api/reviewBar';

	/** A person step whose instructions are this document: the agent's ask, answered Done or Won't do from the reading, through the review ask's shell. */
	type Receipt = 'done' | 'wont_do';
	let {
		personStep,
		receipt = $bindable<Receipt | null>(null),
		suppressed = false,
		capsuleClearance = $bindable(0),
		onAnswered,
		onConflict,
	}: {
		personStep: PersonStepInstruction | null;
		receipt?: Receipt | null;
		suppressed?: boolean;
		capsuleClearance?: number;
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
			failure = 'This task changed. Refreshing the latest state.';
			onConflict?.();
			return;
		}
		failure = outcome.status === 'forbidden'
			? 'You can’t change this task. Ask the agent in chat to change it.'
			: outcome.message;
	}
</script>

<ReviewBarShell
	view={receipt ? 'receipt' : 'ask'}
	who={personStep ? { name: personStep.owner_name, id: personStep.owner_thread_id } : undefined}
	full={personStep ? `Needs you to do this: ${personStep.node_title}` : ''}
	askLabel="Task for you" {busy} status={busy ? 'Recording your answer…' : failure} {suppressed} bind:capsuleClearance
	receipt={receipt ? { icon: receipt === 'done' ? 'check' : 'dash', label: 'Task answered', segments: [receipt === 'done' ? 'Done' : 'Won’t do'] } : null}
>
	{#snippet actions()}
		<div class="rb-actions">
			<Button size="sm" variant="ghost" disabled={busy} onclick={() => void settle('close_node')}>Won’t do</Button>
			<Button size="sm" variant="primary" loading={busy} onclick={() => void settle('mark_done')}>Done</Button>
		</div>
	{/snippet}
</ReviewBarShell>
