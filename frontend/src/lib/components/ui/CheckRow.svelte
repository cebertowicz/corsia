<script lang="ts">
	import type { Snippet } from 'svelte';

	/**
	 * A checklist line: a check circle, what it is, and the action that gets it done. The circle ticks by
	 * itself when the system reports it done. Proposed (pending Product Experience and Steve): there is no
	 * checklist row in the app today. Only setup uses it.
	 */
	interface Props {
		label: string;
		checked: boolean;
		actions?: Snippet;
	}

	const { label, checked, actions }: Props = $props();
</script>

<div class="check-row">
	<span class="circle" class:on={checked} role="img" aria-label={checked ? 'Done' : 'Not done yet'}>
		{#if checked}<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>{/if}
	</span>
	<span class="label">{label}</span>
	{#if actions}<span class="actions">{@render actions()}</span>{/if}
</div>

<style>
	.check-row { display: flex; align-items: center; gap: var(--space-3); min-height: var(--space-8); }
	.circle {
		flex: none; width: var(--space-5); height: var(--space-5); border-radius: var(--radius-circle);
		border: 1.5px solid var(--text-tertiary); display: flex; align-items: center; justify-content: center;
		color: var(--accent-on-fill); transition: background var(--dur-standard) var(--ease-material), border-color var(--dur-standard) var(--ease-material);
	}
	.circle.on { background: var(--accent-fill); border-color: var(--accent-fill); }
	.label { flex: 1; min-width: 0; font-size: var(--text-body); color: var(--text-primary); line-height: var(--lh-normal); }
	.actions { flex: none; display: flex; align-items: center; gap: var(--space-2); }
</style>
