<script lang="ts">
	import { Button, Field, Message } from '$lib/components/ui';
	import BillingEditor from './BillingEditor.svelte';
	import { PRICE_ERROR, formatUsd, parsePriceCents } from './billingFormat';
	import { loadVendorAccounts, recordVendorAccountOverage, setVendorAccountBilling, type VendorAccount } from '$lib/stores/vendorAccounts';

	const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
	const currentYear = new Date().getFullYear();
	const YEARS = Array.from({ length: 6 }, (_, index) => currentYear - index);

	// An account's recorded price and charge day, with a way to record overage for one month.
	const { account }: { account: VendorAccount } = $props();

	const readonly = $derived(account.state === 'removed');
	const label = $derived(account.label.text);

	let overageOpen = $state(false);
	let amount = $state('');
	let monthNumber = $state('');
	let year = $state('');
	let amountError = $state('');
	let monthError = $state('');
	let overageError = $state('');
	let saving = $state(false);
	let recorded = $state('');

	async function save(next: { monthly_price_cents: number | null; charge_day: number | null }) {
		const result = await setVendorAccountBilling(account, next);
		return result.ok ? { ok: true } : { ok: false, error: result.error, conflict: result.code === 'billing_changed' };
	}

	async function reload() { await loadVendorAccounts(); }

	function openOverage() {
		amount = ''; monthNumber = ''; year = ''; amountError = monthError = overageError = '';
		recorded = '';
		overageOpen = true;
	}

	async function saveOverage() {
		amountError = monthError = overageError = '';
		const cents = parsePriceCents(amount);
		if (cents === null) amountError = PRICE_ERROR;
		const month = `${year}-${monthNumber}`;
		if (!monthNumber || !year) monthError = 'Choose a month and year.';
		if (cents === null || monthError) return;
		saving = true;
		const result = await recordVendorAccountOverage(account, { amount_cents: cents, month });
		saving = false;
		if (!result.ok) {
			overageError = result.error ?? 'Could not save overage.';
			return;
		}
		overageOpen = false;
		recorded = `Overage ${formatUsd(cents)} · ${MONTHS[Number(monthNumber) - 1].slice(0, 3)} ${year} recorded.`;
	}
</script>

<div class="account-billing" data-billing-account={account.id}>
	<BillingEditor
		{label}
		billing={account.billing ?? null}
		{readonly}
		anchorId="account-billing"
		onsave={save}
		onreload={reload}
	/>
	{#if !readonly}
		{#if overageOpen}
			<div class="overage">
				<div class="fields">
					<Field label="Overage paid (USD)" layout="stacked" error={amountError}>
						<input inputmode="decimal" aria-label={`Overage paid (USD) for ${label}`} disabled={saving} bind:value={amount} />
					</Field>
					<Field label="Billing month" layout="stacked" error={monthError}>
						<div class="month-year">
							<select aria-label={`Billing month for ${label}`} disabled={saving} bind:value={monthNumber}>
								<option value="">Month</option>
								{#each MONTHS as name, index (name)}<option value={String(index + 1).padStart(2, '0')}>{name}</option>{/each}
							</select>
							<select aria-label={`Billing year for ${label}`} disabled={saving} bind:value={year}>
								<option value="">Year</option>
								{#each YEARS as option (option)}<option value={String(option)}>{option}</option>{/each}
							</select>
						</div>
					</Field>
				</div>
				<div class="actions">
					<Button size="sm" variant="primary" loading={saving} onclick={saveOverage}>Save</Button>
					<Button size="sm" disabled={saving} onclick={() => overageOpen = false}>Cancel</Button>
				</div>
			</div>
			<p class="help">Saving again for the same month replaces it.</p>
			{#if overageError}<Message variant="err">{overageError}</Message>{/if}
		{:else}
			<div class="overage-line">
				<Button size="sm" variant="secondary" onclick={openOverage}>Add overage</Button>
				{#if recorded}<span role="status">{recorded}</span>{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	.overage-line { display: flex; align-items: center; gap: 8px; padding-top: 4px; color: var(--text-secondary); font-size: var(--text-footnote); }
	.help { margin: 0; padding-top: 4px; color: var(--text-secondary); font-size: var(--text-footnote); }
	.overage { display: flex; align-items: end; gap: 12px; flex-wrap: wrap; padding-top: 4px; }
	.fields { display: flex; gap: 12px; flex: 1 1 auto; min-width: 0; }
	.fields :global(.field) { flex: 1 1 0; min-width: 0; padding: 4px 0; border-bottom: 0; gap: 4px; }
	.fields :global(.field .t) { display: none; }
	.month-year { display: flex; gap: 6px; min-width: 0; }
	.month-year select { flex: 1 1 0; min-width: 0; }
	.month-year select:first-child { flex-grow: 2; }
	.actions { display: flex; align-items: center; gap: 6px; padding-bottom: 4px; }
	@media (max-width: 480px) {
		.overage { align-items: stretch; }
		.fields { flex-direction: column; flex-basis: 100%; gap: 0; }
		.actions { padding-bottom: 0; }
	}
</style>
