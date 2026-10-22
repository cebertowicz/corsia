<script lang="ts">
	import Toggle from '../ui/Toggle.svelte';
	import IconButton from '../ui/IconButton.svelte';
	import PolicySaveStatus from './PolicySaveStatus.svelte';

	interface Props {
		accountName: string;
		enabled: boolean;
		disabled?: boolean;
		toggleDisabled?: boolean;
		moveDisabled?: boolean;
		pending?: boolean;
		failure?: string | null;
		position?: number | null;
		total?: number;
		onmove?: (direction: -1 | 1) => void;
		ontoggle: (enabled: boolean) => void;
	}

	let { accountName, enabled, disabled = false, toggleDisabled = disabled, moveDisabled = disabled, pending = false, failure = null, position = null, total = 0, onmove, ontoggle }: Props = $props();
	const showOrder = $derived(position !== null && total > 1 && !!onmove);
	const statusId = $derived(`account-policy-${accountName.replace(/[^a-zA-Z0-9_-]/g, '-')}`);
</script>

<span class="provider-account-policy-control" aria-label={`${accountName} account policy controls`}>
	<span class="provider-account-policy-controls">
		{#if showOrder}
			<IconButton size="sm" ariaLabel={`Move ${accountName} up`} disabled={moveDisabled || position === 0} onclick={() => onmove?.(-1)}><svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6" /></svg></IconButton>
			<IconButton size="sm" ariaLabel={`Move ${accountName} down`} disabled={moveDisabled || position === total - 1} onclick={() => onmove?.(1)}><svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6" /></svg></IconButton>
		{/if}
		<Toggle checked={enabled} disabled={toggleDisabled} ariaLabel={`Use ${accountName}`} describedBy={pending || failure ? statusId : undefined} onchange={ontoggle} />
	</span>
	<PolicySaveStatus id={statusId} {pending} {failure} />
</span>

<style>
	/* The account row owns placement; this component always emits a control plus its status line. */
	.provider-account-policy-control { display: contents; }
	.provider-account-policy-controls { display: flex; align-items: center; gap: var(--space-1); }
</style>
