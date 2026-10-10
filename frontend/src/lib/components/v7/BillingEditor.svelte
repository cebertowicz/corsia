<script lang="ts">
	import { Button, Field, Message } from '$lib/components/ui';
	import { billingSummary, parsePriceCents, parseChargeDay, PRICE_ERROR, DAY_ERROR, LIST_PRICE, NO_LIST_PRICE, type BillingMode, type BillingValue } from './billingFormat';

	// One billing row for every account and External endpoint, so the price, day, copy and layout cannot drift apart.
	// `endpoint` adds the mode choice; without it the row is an account's subscription price.
	interface Props {
		label: string;
		billing: BillingValue | null;
		endpoint?: { mode: BillingMode | null; listPriced: boolean };
		readonly?: boolean;
		/** Marks the row as a target for `data-settings-anchor` navigation. */
		anchorId?: string;
		onsave: (next: { mode?: BillingMode | null; monthly_price_cents: number | null; charge_day: number | null }) => Promise<{ ok: boolean; error?: string; conflict?: boolean }>;
		onreload?: () => Promise<void>;
	}
	const { label, billing, endpoint, readonly = false, anchorId, onsave, onreload }: Props = $props();

	const isSet = $derived(endpoint ? endpoint.mode !== null : billing !== null);
	const summary = $derived(billingSummary(billing, endpoint ? { mode: endpoint.mode, listPriced: endpoint.listPriced } : null));

	let editing = $state(false);
	let draftMode = $state<BillingMode | null>(null);
	let price = $state('');
	let day = $state('');
	let priceError = $state('');
	let dayError = $state('');
	let modeError = $state('');
	let saveError = $state('');
	let conflict = $state(false);
	let saving = $state(false);

	function begin() {
		draftMode = endpoint ? endpoint.mode : 'subscription';
		price = billing ? (billing.monthly_price_cents / 100).toFixed(2) : '';
		day = billing ? String(billing.charge_day) : '';
		priceError = dayError = modeError = saveError = '';
		conflict = false;
		editing = true;
	}

	function cancel() {
		if (saving) return;
		editing = false;
	}

	async function submit(next: { mode?: BillingMode | null; monthly_price_cents: number | null; charge_day: number | null }) {
		saving = true;
		saveError = '';
		const result = await onsave(next);
		saving = false;
		if (result.ok) { editing = false; return; }
		saveError = result.error ?? 'Could not save billing.';
		conflict = !!result.conflict;
	}

	async function save() {
		priceError = dayError = modeError = saveError = '';
		if (endpoint && draftMode === null) { modeError = 'Choose how this endpoint is billed.'; return; }
		if (draftMode === 'pay_per_use') { await submit({ mode: 'pay_per_use', monthly_price_cents: null, charge_day: null }); return; }
		const cents = parsePriceCents(price);
		const chosenDay = parseChargeDay(day);
		if (cents === null) priceError = PRICE_ERROR;
		if (chosenDay === null) dayError = DAY_ERROR;
		if (cents === null || chosenDay === null) return;
		await submit({ ...(endpoint ? { mode: 'subscription' as const } : {}), monthly_price_cents: cents, charge_day: chosenDay });
	}

	async function clear() {
		priceError = dayError = modeError = saveError = '';
		await submit({ ...(endpoint ? { mode: null } : {}), monthly_price_cents: null, charge_day: null });
	}

	async function reload() {
		if (!onreload) return;
		saving = true;
		await onreload();
		saving = false;
		saveError = '';
		conflict = false;
	}

	const showFields = $derived(!endpoint || draftMode === 'subscription');
</script>

<div class="billing" class:ruled={!!endpoint} data-settings-anchor={anchorId} tabindex="-1" aria-label={label} role="group">
	{#if !editing}
		<div class="summary">
			<span class="name">Billing</span>
			<span class="value">{summary.text}</span>
			{#if !readonly}
				<span class="edit"><Button size="sm" onclick={begin}>{isSet ? 'Edit' : 'Set price'}</Button></span>
			{/if}
		</div>
	{:else}
		<div class="editor">
			<span class="name">Billing</span>
			{#if endpoint}
				<div class="modes" role="radiogroup" aria-label={`Billing for ${label}`}>
					<button type="button" role="radio" aria-checked={draftMode === 'subscription'} class:on={draftMode === 'subscription'} disabled={saving} onclick={() => { draftMode = 'subscription'; modeError = ''; }}>Subscription</button>
					<button type="button" role="radio" aria-checked={draftMode === 'pay_per_use'} class:on={draftMode === 'pay_per_use'} disabled={saving} onclick={() => { draftMode = 'pay_per_use'; modeError = ''; }}>Pay per use</button>
				</div>
				{#if modeError}<Message variant="err">{modeError}</Message>{/if}
				{#if draftMode === 'pay_per_use'}
					<p class="note">{endpoint.listPriced ? LIST_PRICE : NO_LIST_PRICE}</p>
				{/if}
			{/if}
			<div class="row">
				{#if showFields}
					<div class="fields">
						<Field label="Monthly price (USD)" layout="stacked" error={priceError}>
							<input inputmode="decimal" aria-label={`Monthly price (USD) for ${label}`} disabled={saving} bind:value={price} onkeydown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void save(); } }} />
						</Field>
						<Field label="Charge day" layout="stacked" error={dayError}>
							<select aria-label={`Charge day for ${label}`} disabled={saving} bind:value={day}>
								<option value="">Choose a day</option>
								{#each Array.from({ length: 31 }, (_, index) => index + 1) as option (option)}<option value={String(option)}>{option}</option>{/each}
							</select>
						</Field>
					</div>
				{/if}
				<div class="actions">
					<Button size="sm" variant="primary" loading={saving} onclick={save}>Save</Button>
					<Button size="sm" disabled={saving} onclick={cancel}>Cancel</Button>
					{#if isSet}<Button size="sm" variant="ghost" disabled={saving} onclick={clear}>Clear</Button>{/if}
				</div>
			</div>
			{#if saveError}
				<Message variant="err">{saveError}</Message>
				{#if conflict && onreload}<div><Button size="sm" disabled={saving} onclick={reload}>Reload</Button></div>{/if}
			{/if}
		</div>
	{/if}
</div>

<style>
	.billing { padding: 8px 0 0; color: var(--text-secondary); font-size: var(--text-footnote); outline: none; }
	.billing.ruled { margin-top: 10px; padding: 10px 0 0; border-top: 1px solid var(--border-subtle); }
	.billing:focus-visible { box-shadow: 0 0 0 3px var(--violet-bg); border-radius: var(--radius-xs); }
	.summary { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; }
	.name { color: var(--text-secondary); }
	.value { color: var(--text-primary); min-width: 0; overflow-wrap: anywhere; }
	.note { margin: 0; color: var(--text-secondary); }
	.edit { margin-left: auto; }
	.editor { display: grid; gap: 8px; }
	.row { display: flex; align-items: end; gap: 12px; flex-wrap: wrap; }
	.fields { display: flex; gap: 12px; flex: 1 1 auto; min-width: 0; }
	.fields :global(.field) { flex: 1 1 0; min-width: 0; padding: 4px 0; border-bottom: 0; gap: 4px; }
	.fields :global(.field .t) { display: none; }
	.actions { display: flex; align-items: center; gap: 6px; padding-bottom: 4px; flex-wrap: wrap; }
	.modes { display: inline-flex; justify-self: start; border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); overflow: hidden; }
	.modes button { font: inherit; font-size: var(--text-footnote); color: var(--text-primary); background: var(--bg-primary); border: 0; padding: 5px 12px; cursor: pointer; }
	.modes button + button { border-left: 1px solid var(--border-subtle); }
	.modes button.on { background: var(--bg-hover); font-weight: 600; }
	@media (max-width: 480px) {
		.edit { margin-left: 0; width: 100%; }
		.fields { flex-direction: column; flex-basis: 100%; gap: 0; }
		.row { align-items: stretch; }
		.actions { padding-bottom: 0; }
	}
</style>
