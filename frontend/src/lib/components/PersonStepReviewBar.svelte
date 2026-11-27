<script lang="ts">
	import { getContext } from 'svelte';
	import { get } from 'svelte/store';
	import { Button } from '$lib/components/ui';
	import ReviewBarShell, { ASKER_CHAT } from './ReviewBarShell.svelte';
	import { token as authToken } from '$lib/stores/auth';
	import { mutateUserTask } from '$lib/api/plans';
	import type { PersonStepInstruction } from '$lib/api/reviewBar';
	import { personStepAsk } from './comments/commentTask';

	/**
	 * A person step whose instructions are this document: the agent's ask, answered Done or Won't do from the reading, through the review ask's shell.
	 * A document's comment task is not answered here: CommentTaskDismiss says what waits and offers Dismiss all.
	 */
	type Receipt = 'done' | 'wont_do';
	let {
		personStep,
		receipt = $bindable<Receipt | null>(null),
		suppressed = false,
		docked = false,
		capsuleClearance = $bindable(0),
		onAnswered,
		onConflict,
	}: {
		personStep: PersonStepInstruction | null;
		receipt?: Receipt | null;
		suppressed?: boolean;
		/** Answered from the card docked to the document canvas's bottom edge (ReviewBarShell). */
		docked?: boolean;
		capsuleClearance?: number;
		onAnswered?: () => void;
		onConflict?: () => void;
	} = $props();

	let action = $state<'idle' | 'sending'>('idle');
	let failure = $state('');
	const busy = $derived(action === 'sending');
	const asks = $derived(personStep ? personStepAsk(personStep) : '');
	// Where the asker is a chat link (Activity) the bar reads "Task for you"; the step's own ask opens only when it says more than the title.
	const inActivity = getContext<unknown>(ASKER_CHAT) !== undefined;
	const more = $derived((personStep?.node_description ?? '').trim() !== '' && (personStep?.node_description ?? '').trim() !== personStep?.node_title.trim());
	const line = $derived(inActivity ? (more ? 'Task for you' : '') : '');
	const full = $derived(!inActivity ? asks : more ? (personStep?.node_description ?? '').trim() : 'Task for you');

	async function settle(operation: 'mark_done' | 'close_node') {
		const step = personStep;
		const bearer = get(authToken);
		if (!step || !bearer || busy) return;
		action = 'sending';
		failure = '';
		const outcome = await mutateUserTask(step.plan_id, operation, step.node_id, step.node_revision,
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
	ask={line} {full} {docked}
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
