<script lang="ts">
	import { Button, Modal } from './ui';

	let {
		open = false,
		title,
		description,
		confirmLabel = 'Confirm',
		destructive = false,
		icon,
		onConfirm,
		onCancel,
	}: {
		open?: boolean;
		title: string;
		description: string;
		confirmLabel?: string;
		destructive?: boolean;
		icon?: 'delete';
		onConfirm: () => void;
		onCancel: () => void;
	} = $props();
</script>

{#if open}
	<Modal title={title} size="sm" role="alertdialog" onClose={onCancel}>
		<div class="body">
			{#if icon}
				<div class="icon" class:destructive>
					{#if icon === 'delete'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
					{/if}
				</div>
			{/if}
			<p class="desc">{description}</p>
		</div>

		{#snippet foot()}
			<Button variant="ghost" onclick={onCancel}>Cancel</Button>
			<span style="flex: 1"></span>
			<Button variant={destructive ? 'danger' : 'primary'} onclick={onConfirm}>{confirmLabel}</Button>
		{/snippet}
	</Modal>
{/if}

<style>
	.body { display: flex; flex-direction: column; gap: 14px; }
	.icon {
		width: 40px; height: 40px; border-radius: var(--radius-sm);
		background: var(--accent-wash); color: var(--accent-text, var(--accent));
		display: flex; align-items: center; justify-content: center;
	}
	.icon.destructive {
		background: var(--red-bg); color: var(--red);
	}
	.desc {
		font-size: var(--text-subhead); color: var(--text-secondary);
		margin: 0; line-height: 1.5;
	}
</style>
