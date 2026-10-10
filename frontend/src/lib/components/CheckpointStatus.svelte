<script lang="ts">
	import { token } from '$lib/stores/auth';
	import {
		cancelCheckpoint,
		checkpointStatusCopy,
		type Checkpoint,
	} from '$lib/stores/checkpoints';
	import { toasts } from '$lib/stores/toast';

	let {
		checkpoint,
		threadId,
		placement = 'sheet',
	}: {
		checkpoint: Checkpoint;
		threadId?: string | null;
		placement?: 'sheet' | 'transcript';
	} = $props();

	const cancellable = $derived(
		checkpoint.status === 'running' || checkpoint.status === 'captured',
	);
	let busy = $state(false);

	async function cancel(): Promise<void> {
		if (busy || !cancellable || !threadId || !$token) return;
		busy = true;
		try {
			await cancelCheckpoint(threadId, checkpoint.id, $token);
		} catch (error) {
			toasts.show(
				error instanceof Error ? error.message : 'Could not cancel checkpoint',
				'error',
			);
		} finally {
			busy = false;
		}
	}
</script>

<div
	class="checkpoint-status"
	class:transcript={placement === 'transcript'}
	class:failed={checkpoint.status === 'failed'}
	data-checkpoint-status={checkpoint.status}
>
	<span>{checkpointStatusCopy(checkpoint)}</span>
	{#if cancellable}
		<button
			type="button"
			data-checkpoint-cancel={checkpoint.id}
			disabled={busy}
			onclick={cancel}
		>Cancel</button>
	{/if}
</div>

<style>
	.checkpoint-status {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		min-height: var(--space-6);
		margin: var(--space-1) 0;
		color: var(--text-secondary);
		font-size: var(--text-footnote);
	}
	.checkpoint-status.transcript {
		justify-content: center;
		gap: var(--space-2);
		max-width: 760px;
		margin: 0 auto var(--space-3);
		color: var(--text-tertiary);
	}
	.checkpoint-status.failed { color: var(--red-text); }
	button {
		border: 0;
		background: none;
		color: inherit;
		font: inherit;
		font-weight: 600;
		cursor: pointer;
	}
	button:hover:not(:disabled) { color: var(--text-primary); }
	button:disabled { opacity: 0.55; cursor: default; }
</style>
