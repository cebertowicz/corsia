<script lang="ts">
	import Toggle from '../ui/Toggle.svelte';
	import IconButton from '../ui/IconButton.svelte';

	interface Props {
		accountName: string;
		enabled: boolean;
		disabled?: boolean;
		position?: number | null;
		total?: number;
		onmove?: (direction: -1 | 1) => void;
		ontoggle: (enabled: boolean) => void;
	}

	let { accountName, enabled, disabled = false, position = null, total = 0, onmove, ontoggle }: Props = $props();
	const showOrder = $derived(position !== null && total > 1 && !!onmove);
</script>

<span class="provider-account-policy-controls" aria-label={`${accountName} account policy controls`}>
	{#if showOrder}
		<IconButton size="sm" ariaLabel={`Move ${accountName} up`} disabled={disabled || position === 0} onclick={() => onmove?.(-1)}><svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6" /></svg></IconButton>
		<IconButton size="sm" ariaLabel={`Move ${accountName} down`} disabled={disabled || position === total - 1} onclick={() => onmove?.(1)}><svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6" /></svg></IconButton>
	{/if}
	<Toggle checked={enabled} {disabled} ariaLabel={`Use ${accountName}`} onchange={ontoggle} />
</span>

<style>
	.provider-account-policy-controls { display: flex; align-items: center; gap: var(--space-1); }
</style>
