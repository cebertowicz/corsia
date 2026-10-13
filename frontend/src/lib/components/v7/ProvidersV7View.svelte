<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { SvelteSet } from 'svelte/reactivity';
	import {
		harnessInventoryError,
		hostGroups,
		hostGroupsLoading,
		liveHostGroups,
		loadHostGroups,
		cloudProviderOn,
		providerEnabled,
	} from '$lib/stores/providersV7';
	import { providers, loadProviders, updateProvider } from '$lib/stores/providers';
	import { loadNetworkServers, setNetworkServerEnabled } from '$lib/stores/networkServers';
	import { Message } from '$lib/components/ui';
	import { loadModels } from '$lib/stores/models';
	import { loadVendorAccounts, vendorAccounts } from '$lib/stores/vendorAccounts';
	import { providerCardRequest, settingsAnchorRequest } from '$lib/stores/settingsNavigation';
	import HostSection from './HostSection.svelte';
	import ProviderRow from './ProviderRow.svelte';
	import ProviderModelsBody from './ProviderModelsBody.svelte';
	import ProviderSettingsModal from './ProviderSettingsModal.svelte';
	import AddProviderModal from './AddProviderModal.svelte';
	import type { HostProvider } from '$lib/stores/providersV7';
	import type { ProviderEntry } from '$lib/stores/providers';

	const expanded = new SvelteSet<string>();
	let openProvider = $state<HostProvider | null>(null);
	let showAdd = $state(false);
	let toggleError = $state('');

	function _hostProviderForRaw(p: ProviderEntry): HostProvider {
		return {
			name: p.name,
			label: p.label,
			type: p.type,
			statusDot: cloudProviderOn(p) ? 'online' : 'disabled',
			subline: '',
			raw: p,
		};
	}

	function rowKey(hostKey: string, providerName: string, serverId?: string): string {
		return `${hostKey}|${providerName}|${serverId ?? ''}`;
	}

	function toggleExpand(key: string) {
		if (expanded.has(key)) expanded.delete(key);
		else expanded.add(key);
	}

	async function handleToggleEnable(p: HostProvider) {
		const enabled = providerEnabled(p);
		toggleError = '';
		const result: { ok: boolean; message?: string; error?: string } = p.server?.kind === 'external'
			? await setNetworkServerEnabled(p.server.id, !enabled)
			: await updateProvider(p.name, { enabled: !enabled });
		if (!result.ok) {
			const reason = result.message ?? result.error;
			toggleError = `Could not turn ${p.label} ${enabled ? 'off' : 'on'}: ${reason ?? 'try again.'}`;
		}
		await loadHostGroups();
		loadModels();
	}

	function handleSettings(p: HostProvider) {
		openProvider = p;
	}

	function accentFor(p: HostProvider): string {
		if (p.name === 'omlx') return 'var(--violet)';
		if (p.name === 'lmstudio') return 'var(--green)';
		if (p.type === 'cloud' || p.type === 'harness') return 'var(--violet)';
		return 'var(--accent)';
	}

	function hasLoaded(p: HostProvider): boolean {
		if (p.type === 'cloud') {
			return p.raw.has_key === true;
		}
		if (p.type === 'local') {
			return false;
		}
		return p.statusDot === 'online';
	}

	onMount(async () => {
		await Promise.all([loadProviders(), loadNetworkServers(), loadVendorAccounts()]);
		await loadHostGroups();
		loadModels();
	});

	const groups = $derived(
		$hostGroups.length > 0
			? $hostGroups
			: $liveHostGroups,
	);

	// The chat held line's "Spend past plan limit" and Settings › Keys land here: open
	// that card (a harness account or a cloud provider), then bring it into view. It never toggles.
	$effect(() => {
		const connection = $providerCardRequest;
		if (!connection) return;
		const matches = (p: { name: string; server?: { id: string } }) => p.name === connection || p.server?.id === connection;
		const group = groups.find((g) => g.providers.some(matches));
		const card = group?.providers.find(matches);
		if (!group || !card) return;
		expanded.add(rowKey(group.key, card.name, card.server?.id));
		providerCardRequest.set(null);
		void tick().then(() => document.querySelector(`[data-provider-card="${connection}"], [data-server-card="${connection}"]`)?.scrollIntoView({ block: 'start' }));
	});

	// A link to an account's billing row opens the settings of the provider that holds it
	// (the first account without a price, else the first account), then focuses the row.
	$effect(() => {
		if ($settingsAnchorRequest !== 'account-billing') return;
		const live = $vendorAccounts.filter((account) => account.state !== 'removed');
		const target = live.find((account) => !account.billing) ?? live[0];
		const provider = target && groups.flatMap((g) => g.providers).find((p) => p.type === 'harness' && p.name === target.provider);
		if (!target || !provider) return;
		openProvider = provider;
		void tick().then(tick).then(() => document.querySelector<HTMLElement>(`[data-billing-account="${target.id}"] [data-settings-anchor]`)?.focus());
	});
</script>

<header class="page-head">
	<h2>Providers</h2>
	<button type="button" class="add-btn" onclick={() => showAdd = true}>
		<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
			<path d="M12 5v14M5 12h14"/>
		</svg>
		Add provider
	</button>
</header>

{#if toggleError}<Message variant="err">{toggleError}</Message>{/if}
{#if $harnessInventoryError}<Message variant="err">{$harnessInventoryError}</Message>{/if}

{#if $hostGroupsLoading && groups.length === 0}
	<p class="loading">Loading providers…</p>
{:else if groups.length === 0}
	<div class="empty">
		<div class="empty-title">No providers configured</div>
		<div class="empty-hint">Add a local engine or a cloud API key to get started.</div>
		<button type="button" class="add-btn primary" onclick={() => showAdd = true}>
			<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
				<path d="M12 5v14M5 12h14"/>
			</svg>
			Add your first provider
		</button>
	</div>
{:else}
	{#each groups as group (group.key)}
		<HostSection
			{group}
			isThisMac={group.key === 'this-mac'}
			isCloud={group.key === 'cloud'}
		>
			{#each group.providers as p (rowKey(group.key, p.name, p.server?.id))}
				{@const key = rowKey(group.key, p.name, p.server?.id)}
				<ProviderRow
					provider={p}
					expanded={expanded.has(key)}
					hasLoaded={hasLoaded(p)}
					accentVar={accentFor(p)}
					onToggleEnable={() => handleToggleEnable(p)}
					onSettings={() => handleSettings(p)}
					onToggleExpand={() => toggleExpand(key)}
				>
					{#snippet body()}
						<ProviderModelsBody provider={p} isLocal={group.key === 'this-mac'} />
					{/snippet}
				</ProviderRow>
			{/each}
		</HostSection>
	{/each}
{/if}

{#if openProvider}
	<ProviderSettingsModal
		provider={openProvider}
		onClose={() => openProvider = null}
	/>
{/if}

{#if showAdd}
	<AddProviderModal
		onClose={() => showAdd = false}
		onSelectCloud={(p) => { openProvider = _hostProviderForRaw(p); }}
	/>
{/if}

<style>
	.page-head {
		display: flex; align-items: center; justify-content: space-between;
		gap: 16px;
		margin-bottom: 18px;
	}
	.page-head h2 {
		font-size: var(--text-title2); font-weight: 600; letter-spacing: -0.015em;
		color: var(--text-primary);
		margin: 0;
	}

	.add-btn {
		display: inline-flex; align-items: center; gap: 6px;
		height: 32px; padding: 0 13px;
		color: var(--text-secondary); font-size: var(--text-subhead); font-weight: 500;
		background: var(--bg-secondary);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		cursor: pointer;
		font-family: inherit;
		transition: all var(--dur-tight) var(--ease-snap);
	}
	.add-btn:hover {
		color: var(--violet-text);
		border-color: var(--violet-line);
		background: var(--violet-bg);
	}
	.add-btn.primary {
		background: var(--violet-bg);
		border-color: var(--violet-line);
		color: var(--violet-text);
		margin-top: 12px;
	}

	.loading {
		font-size: var(--text-subhead);
		color: var(--text-muted);
		padding: 12px 0;
	}

	.empty {
		display: flex; flex-direction: column; align-items: center; gap: 10px;
		padding: 32px 24px;
		color: var(--text-tertiary); font-size: var(--text-subhead);
	}
	.empty-title { font-size: var(--text-subhead); color: var(--text-secondary); font-weight: 500; }
	.empty-hint { font-size: var(--text-footnote); color: var(--text-tertiary); }

	@media (max-width: 480px) {
		.page-head { margin-bottom: 14px; }
		.page-head h2 { font-size: var(--text-title2); }
		.add-btn { height: 30px; padding: 0 11px; font-size: var(--text-footnote); }
	}
</style>
