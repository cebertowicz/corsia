<script lang="ts">
	import { Button, Field, Message } from '$lib/components/ui';
	import { parseModelIds, setNetworkServerBilling, updateNetworkServer, type NetworkServer } from '$lib/stores/networkServers';
	import BillingEditor from './BillingEditor.svelte';
	import type { BillingMode } from './billingFormat';
	import { loadHostGroups } from '$lib/stores/providersV7';

	interface Props {
		server: NetworkServer;
	}

	const { server }: Props = $props();
	let baseUrl = $state('');
	let dialect = $state<'openai' | 'anthropic'>('openai');
	let modelIds = $state('');
	let apiKey = $state('');
	let initialized = false;

	$effect(() => {
		if (initialized) return;
		baseUrl = server.metadata?.base_url ?? '';
		dialect = server.dialect ?? 'openai';
		modelIds = (server.metadata?.declared_models ?? []).join('\n');
		initialized = true;
	});
	let error = $state('');
	let message = $state('');
	let busy = $state(false);

	const subscription = $derived(server.billing?.mode === 'subscription' && server.billing.monthly_price_cents !== null && server.billing.charge_day !== null
		? { monthly_price_cents: server.billing.monthly_price_cents, charge_day: server.billing.charge_day } : null);

	async function saveBilling(next: { mode?: BillingMode | null; monthly_price_cents: number | null; charge_day: number | null }) {
		const result = await setNetworkServerBilling(server, { mode: next.mode ?? null, monthly_price_cents: next.monthly_price_cents, charge_day: next.charge_day });
		if (!result.ok) return { ok: false, error: result.message, conflict: result.code === 'billing_changed' };
		// The open card reads the host-group snapshot, which the store write alone does not refresh.
		await loadHostGroups();
		return { ok: true };
	}

	async function save() {
		error = '';
		message = '';
		const endpoint = baseUrl.trim();
		if (!/^https?:\/\/[^/]+/i.test(endpoint)) {
			error = 'Enter a full http(s) endpoint URL.';
			return;
		}
		busy = true;
		const result = await updateNetworkServer(server.id, {
			base_url: endpoint,
			// Always sent, so emptying the field clears the declared ids.
			model_ids: parseModelIds(modelIds),
			dialect,
			...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
		});
		busy = false;
		if (result.ok) {
			apiKey = '';
			message = 'Endpoint settings saved; rediscovering models.';
			await loadHostGroups();
		} else {
			error = result.message ?? 'Failed to save endpoint settings';
		}
	}
</script>

<Field label="Endpoint URL">
	<input placeholder="Full provider endpoint URL" bind:value={baseUrl} />
</Field>
<Field label="API dialect">
	<select bind:value={dialect} aria-label="API dialect">
		<option value="openai">OpenAI-compatible</option>
		<option value="anthropic">Anthropic-compatible</option>
	</select>
</Field>
<Field label="Model IDs" hint="only if the endpoint doesn't list its models, as Alibaba's Token Plan and Coding Plan don't. One per line or comma-separated.">
	<textarea rows="3" aria-label="Model IDs" placeholder="glm-5.3, deepseek-v4.1-flash" bind:value={modelIds}></textarea>
</Field>
<Field label="API key">
	<input type="password" placeholder={server.has_key ? '••••••••  (saved; replace)' : 'Optional API key'} bind:value={apiKey} />
</Field>
{#if error}<Message variant="err">{error}</Message>{/if}
{#if message}<Message variant="ok">{message}</Message>{/if}
<Button variant="primary" disabled={busy} onclick={save}>
	{busy ? 'Saving…' : 'Save endpoint settings'}
</Button>
<div class="billing-inset">
	<BillingEditor
		label={server.instance_name}
		billing={subscription}
		endpoint={{ mode: server.billing?.mode ?? null, listPriced: server.list_priced ?? false }}
		onsave={saveBilling}
		onreload={loadHostGroups}
	/>
</div>

<style>
	/* Line the billing row's right edge up with the fields above, which end 14px short of the card (Field's empty trailing column). */
	.billing-inset { padding-right: 14px; }
</style>
