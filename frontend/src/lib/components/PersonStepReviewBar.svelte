<script lang="ts">
	import { get } from 'svelte/store';
	import { Button } from '$lib/components/ui';
	import ReviewBarShell from './ReviewBarShell.svelte';
	import { token as authToken } from '$lib/stores/auth';
	import { mutateUserTask } from '$lib/api/plans';
	import { dismissCommentAttention } from '$lib/api/docs';
	import { feedbackLine } from '$lib/api/messageCards';
	import type { PersonStepInstruction } from '$lib/api/reviewBar';
	import { DISMISS_ALL, isCommentTask } from './comments/commentTask';

	/**
	 * A person step whose instructions are this document: the agent's ask, answered Done or Won't do from the reading, through the review ask's shell.
	 * A document's comment task is not answered: it says what waits and offers only the plan menu's Dismiss all; reading and deciding clear it.
	 */
	type Receipt = 'done' | 'wont_do';
	let {
		personStep,
		receipt = $bindable<Receipt | null>(null),
		suppressed = false,
		capsuleClearance = $bindable(0),
		onAnswered,
		onConflict,
		onDismissed,
	}: {
		personStep: PersonStepInstruction | null;
		receipt?: Receipt | null;
		suppressed?: boolean;
		capsuleClearance?: number;
		onAnswered?: () => void;
		onConflict?: () => void;
		/** The comment task's items were marked read; the bar re-reads and leaves once its task settles. */
		onDismissed?: () => void;
	} = $props();

	let action = $state<'idle' | 'sending'>('idle');
	let failure = $state('');
	const busy = $derived(action === 'sending');
	const commentTask = $derived(isCommentTask(personStep));
	const waiting = $derived(feedbackLine(personStep?.comment_attention ?? {}));

	async function dismiss() {
		const attention = personStep?.comment_attention;
		if (!attention || busy) return;
		action = 'sending';
		failure = '';
		try {
			await dismissCommentAttention(attention.artifact_id);
			onDismissed?.();
		} catch {
			failure = 'Could not dismiss comments. Try again.';
		} finally {
			action = 'idle';
		}
	}

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
	full={!personStep ? '' : commentTask ? (waiting ? `${waiting} to review` : '') : `Needs you to do this: ${personStep.node_title}`}
	askLabel="Task for you" {busy} status={busy ? (commentTask ? 'Dismissing…' : 'Recording your answer…') : failure} {suppressed} bind:capsuleClearance
	receipt={receipt ? { icon: receipt === 'done' ? 'check' : 'dash', label: 'Task answered', segments: [receipt === 'done' ? 'Done' : 'Won’t do'] } : null}
>
	{#snippet actions()}
		<div class="rb-actions">
			{#if commentTask}
				<Button size="sm" variant="ghost" loading={busy} onclick={() => void dismiss()}>{DISMISS_ALL}</Button>
			{:else}
				<Button size="sm" variant="ghost" disabled={busy} onclick={() => void settle('close_node')}>Won’t do</Button>
				<Button size="sm" variant="primary" loading={busy} onclick={() => void settle('mark_done')}>Done</Button>
			{/if}
		</div>
	{/snippet}
</ReviewBarShell>
