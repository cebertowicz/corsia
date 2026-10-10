<script lang="ts">
	import { Message, Toggle } from '$lib/components/ui';
	import { loadModels } from '$lib/stores/models';
	import { OVERAGE_SWITCH_LABEL } from '$lib/stores/overageSetting';
	import { setVendorAccountOverage, type VendorAccount } from '$lib/stores/vendorAccounts';

	// One component for every account row, so the switch and its label cannot drift apart.
	// `inline` sits inside an account row; otherwise it is the card's own row (one account).
	const { account, inline = false }: { account: VendorAccount; inline?: boolean } = $props();

	let saving = $state(false);
	let error = $state('');
	const allowed = $derived(account.allow_overage ?? null);
	const accountLabel = $derived(account.label.text);

	async function change(next: boolean) {
		error = '';
		saving = true;
		const result = await setVendorAccountOverage(account, next);
		// The cards' existing toggle-failure template, so every switch fails the same way.
		if (!result.ok) error = `Could not turn ${OVERAGE_SWITCH_LABEL} ${next ? 'on' : 'off'}: ${result.error}`;
		await loadModels();
		saving = false;
	}
</script>

<div class="overage-row" class:inline>
	<span class="overage-label">{OVERAGE_SWITCH_LABEL}</span>
	<Toggle
		checked={allowed === true}
		onchange={change}
		ariaLabel={inline ? `${OVERAGE_SWITCH_LABEL} for ${accountLabel}` : OVERAGE_SWITCH_LABEL}
		disabled={saving || allowed === null}
	/>
</div>
{#if error}<Message variant="err">{error}</Message>{/if}

<style>
	.overage-row {
		display: flex; align-items: center; justify-content: space-between;
		gap: 12px;
		padding: 10px 14px;
		border-bottom: 1px solid var(--border-subtle);
	}
	.overage-row.inline { padding: 8px 0 0; border-bottom: 0; }
	.overage-label { color: var(--text-primary); font-size: var(--text-subhead); font-weight: 500; }
	.inline .overage-label { color: var(--text-secondary); font-size: var(--text-footnote); font-weight: 400; }
	@media (max-width: 480px) {
		.overage-row:not(.inline) { padding: 10px 12px; }
		.overage-label { font-size: var(--text-footnote); }
	}
</style>
