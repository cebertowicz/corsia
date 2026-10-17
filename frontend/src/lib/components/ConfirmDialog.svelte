<script lang="ts">
	import { Button, Modal } from './ui';

	let {
		open = false,
		title,
		description,
		confirmLabel = 'Confirm',
		destructive = false,
		onConfirm,
		onCancel,
	}: {
		open?: boolean;
		title: string;
		description: string;
		confirmLabel?: string;
		destructive?: boolean;
		onConfirm: () => void;
		onCancel: () => void;
	} = $props();
</script>

{#if open}
	<Modal title={title} size="sm" role="alertdialog" onClose={onCancel}>
		<p class="desc">{description}</p>

		{#snippet foot()}
			<Button variant="ghost" onclick={onCancel}>Cancel</Button>
			<Button variant={destructive ? 'danger' : 'primary'} onclick={onConfirm}>{confirmLabel}</Button>
		{/snippet}
	</Modal>
{/if}

<style>
	.desc {
		font-size: var(--text-subhead); color: var(--text-secondary);
		margin: 0; line-height: 1.5;
	}
</style>
