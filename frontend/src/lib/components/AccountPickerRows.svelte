<script lang="ts">
	import { activeThreadId } from '$lib/stores/threads';
	import { choosePickerAccount } from '$lib/stores/pickerAccountChoice';
	import { activeModelId } from '$lib/stores/models';
	import { workspaceConductorId } from '$lib/utils/conductor';
	import type { PickerAccount } from '$lib/stores/accountPicker';
	import AccountUsageRow from './v7/AccountUsageRow.svelte';

	/**
	 * A vendor's accounts under its group header in the model picker, one compact row each,
	 * through the one shared account row. The caller passes rows only for a vendor with two or
	 * more accounts; one account has no row, so no account chrome. Choosing a row pins the open
	 * thread to that account; during a running turn it applies at the next turn, as a model choice
	 * does. A draft chat has no thread, so its rows are read-only.
	 */
	// Automatic rollover still shows every permitted account in server order. Its
	// header names the account currently in use, so the list deliberately has no
	// duplicate selected fill.
	let { rows, suppressMarked = false }: { rows: PickerAccount[]; suppressMarked?: boolean } = $props();

	let error = $state('');
	let busy = false;

	async function choose(row: PickerAccount) {
		const threadId = $activeThreadId;
		if (!threadId || row.marked || !row.choosable || busy) return;
		busy = true;
		error = '';
		const result = await choosePickerAccount(threadId, $activeModelId, $workspaceConductorId, row);
		busy = false;
		if (!result.ok) error = result.message;
	}
</script>

<div class="account-rows" role="group" aria-label="Accounts">
	{#each rows as row (row.account.id)}
		<AccountUsageRow
			account={row.account}
			variant="picker"
			marked={suppressMarked ? false : row.marked}
			pending={row.pending}
			order={row.order}
			choosable={row.choosable}
			onselect={$activeThreadId ? () => choose(row) : undefined}
		/>
	{/each}
	{#if error}<p class="account-error" role="alert">{error}</p>{/if}
</div>

<style>
	.account-rows { display: flex; flex-direction: column; gap: 1px; margin-bottom: 4px; }
	.account-error { margin: 2px 0 0; padding: 0 10px 0 32px; font-size: var(--text-footnote); color: var(--red-text); }
</style>
