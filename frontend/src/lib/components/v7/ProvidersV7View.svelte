<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { get } from 'svelte/store';
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
	import { Button } from '$lib/components/ui';
	import { loadModels } from '$lib/stores/models';
	import { loadVendorAccounts, vendorAccounts } from '$lib/stores/vendorAccounts';
	import { providerCardRequest, settingsNavigation } from '$lib/stores/settingsNavigation';
	import HostSection from './HostSection.svelte';
	import ProviderRow from './ProviderRow.svelte';
	import ProviderModelsBody from './ProviderModelsBody.svelte';
	import ProviderSettingsModal from './ProviderSettingsModal.svelte';
	import AddProviderModal from './AddProviderModal.svelte';
	import type { HostGroup, HostProvider } from '$lib/stores/providersV7';
	import type { ProviderEntry } from '$lib/stores/providers';
	import { token } from '$lib/stores/auth';
	import { fetchSources, recheckSources, localPickerStatus, localSetupFailed, type ModelSource } from '$lib/conductorSources';
	import { retryLocalSetup } from '$lib/stores/localPickerStatus';
	import { LOW_MEMORY_LOCAL_MODEL_SENTENCE } from '$lib/setupLocalPick.svelte';
	import { on } from '$lib/ws/events';
	import SetupAiRows from '../SetupAiRows.svelte';
	import { createSetupConnect, POLL_MS, VENDOR_COPY } from '$lib/setupConnect.svelte';
	import { withReportAction } from '$lib/setupReport';
	import { HARNESSES } from '$lib/stores/harnessMembership';

	const expanded = new SvelteSet<string>();
	let openProvider = $state<HostProvider | null>(null);
	let showAdd = $state(false);
	let toggleError = $state('');
	let sources = $state<ModelSource[]>([]);
	// Cards are known once the first host-group read has finished; until then a tool may already have one.
	let groupsRead = $state(false);
	const localSource = $derived(sources.find((source) => source.source === 'omlx'));
	let localSetupBusy = $state(false);
	let stopLocalSourceUpdates: (() => void) | null = null;

	async function refreshSources(strict = false) {
		const current = get(token);
		if (!current) return;
		try {
			const existed = localSource?.corsia_omlx === true;
			sources = await (strict ? recheckSources(current) : fetchSources(current));
			// CORSIA's oMLX registers while setup runs: its row replaces Set up the moment it exists.
			if (!existed && localSource?.corsia_omlx === true) void loadHostGroups();
		} catch { /* Keep the last projected state; a failed read must not invent a setup action. */ }
	}

	/** Set up: CORSIA starts its own oMLX; memory, not another oMLX, decides what loads. */
	async function startLocalSetup() {
		if (localSetupBusy || localSource?.eligible === false) return;
		localSetupBusy = true;
		toggleError = '';
		try {
			const result = await retryLocalSetup('setup');
			if (!result.ok) toggleError = result.neverLocalHost ? result.message ?? "Couldn’t set up the local model. Try again." : "Couldn’t set up the local model. Try again.";
		} finally {
			await Promise.all([refreshSources(), loadHostGroups()]);
			loadModels();
			localSetupBusy = false;
		}
	}

	/** Setup runs after the click returns, so the row reads the source for its progress or its failure. */
	const setupFailed = $derived(localSetupFailed(localSource));
	const setupRunning = $derived(['starting', 'downloading'].includes(localSource?.local_state ?? ''));
	const setupLine = $derived.by(() => {
		if (setupFailed) return localSource?.fix || "CORSIA couldn’t set up the local model. Press Retry.";
		if (setupRunning) return localPickerStatus(localSource)?.line ?? 'Setting up…';
		return 'This computer';
	});

	const baseGroups = $derived($hostGroups.length > 0 ? $hostGroups : $liveHostGroups);
	// CORSIA's own oMLX, online or offline, is the local model: Set up shows only without one, and never on a server that cannot run it.
	const localSetupRow = $derived.by<HostProvider | null>(() => {
		if (!localSource || localSource.corsia_omlx === true || localSource.never_local === true) return null;
		return {
			name: 'local-setup', label: 'Local model', type: 'local', statusDot: setupFailed ? 'error' : 'disabled', subline: setupLine,
			raw: { name: 'local-setup', label: 'Local model' } as ProviderEntry,
		};
	});

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
		await Promise.all([loadProviders(), loadNetworkServers(), loadVendorAccounts(), refreshSources()]);
		await loadHostGroups();
		groupsRead = true;
		loadModels();
		stopLocalSourceUpdates = on('models_updated', () => { void refreshSources(); if (cliRows.length) void loadHostGroups(); });
	});
	onDestroy(() => stopLocalSourceUpdates?.());

	// Claude Code and Codex get a card only once they have models. Until then the setup flow's own row
	// installs and signs them in, so a tool skipped or failed in setup is finished here.
	const connectFlow = createSetupConnect({ token: () => get(token), sources: () => sources, refreshSources: async (strict) => { await refreshSources(strict); void loadHostGroups(); } });
	const cliRows = $derived(HARNESSES.flatMap(({ name }) => {
		const source = sources.find((candidate) => candidate.source === name);
		if (!groupsRead || $harnessInventoryError || !source || baseGroups.some((group) => group.providers.some((provider) => provider.name === name))) return [];
		const { cli } = VENDOR_COPY[name], view = connectFlow.row(source);
		return [withReportAction({ key: name, name: cli, ...view }, () => [{ key: name as 'claude-code' | 'codex', name: cli, reference: view.reference, cause: connectFlow.attempts[name]?.cause }])];
	}));
	$effect(() => {
		if (!connectFlow.active) return;
		const timer = setInterval(() => void connectFlow.tick(), POLL_MS);
		return () => clearInterval(timer);
	});

	const groups = $derived.by<HostGroup[]>(() => {
		const base = baseGroups;
		if (!localSetupRow) return base;
		const local = base.find((group) => group.key === 'this-mac');
		if (local) return base.map((group) => group.key === 'this-mac'
			? { ...group, providers: [...group.providers, localSetupRow] }
			: group,
		);
		return [{ key: 'this-mac', name: 'This computer', subname: '', netKind: 'localhost', providers: [localSetupRow] }, ...base];
	});

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
		if ($settingsNavigation.anchor !== 'account-billing') return;
		const live = $vendorAccounts.filter((account) => account.state !== 'removed');
		const target = live.find((account) => !account.billing) ?? live[0];
		const provider = target && groups.flatMap((g) => g.providers).find((p) => p.type === 'harness' && p.name === target.provider);
		if (!target || !provider) return;
		openProvider = provider;
		void tick().then(tick).then(() => document.querySelector<HTMLElement>(`[data-billing-account="${target.id}"] [data-settings-anchor]`)?.focus());
	});
</script>

<header class="page-head">
	<h2 class="page-title">Providers</h2>
	<Button variant="ghost" size="sm" onclick={() => showAdd = true}>Add provider</Button>
</header>

{#if toggleError}<p class="error" role="alert">{toggleError}</p>{/if}
{#if $harnessInventoryError}<p class="error" role="alert">{$harnessInventoryError}</p>{/if}

{#if $hostGroupsLoading && groups.length === 0}
	<p class="loading">Loading providers…</p>
{:else if groups.length === 0 && cliRows.length === 0}
	<div class="empty">
		<div class="empty-title">No providers configured</div>
		<div class="empty-hint">Add a local engine or a cloud API key to get started.</div>
		<Button onclick={() => showAdd = true}>Add your first provider</Button>
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
				{@const isLocalSetup = p.name === 'local-setup'}
				{@const setupLabel = isLocalSetup
					? localSource?.eligible === false
						? LOW_MEMORY_LOCAL_MODEL_SENTENCE
						: localSetupBusy || setupRunning ? 'Setting up…' : setupFailed ? 'Retry' : 'Set up'
					: undefined}
				<ProviderRow
					provider={p}
					expanded={!isLocalSetup && expanded.has(key)}
					hasLoaded={hasLoaded(p)}
					{setupLabel}
					onSetup={isLocalSetup && localSource?.eligible !== false && !localSetupBusy && !setupRunning ? () => startLocalSetup() : undefined}
					onToggleEnable={() => handleToggleEnable(p)}
					onSettings={() => handleSettings(p)}
					onToggleExpand={() => { if (!isLocalSetup) toggleExpand(key); }}
				>
					{#snippet body()}
						{#if !isLocalSetup}<ProviderModelsBody provider={p} isLocal={group.key === 'this-mac'} onAddProvider={() => showAdd = true} />{/if}
					{/snippet}
				</ProviderRow>
			{/each}
		</HostSection>
	{/each}
{/if}

{#if cliRows.length}<SetupAiRows rows={cliRows} />{/if}

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
	/* The page title as on every Settings page, with the page's one action at the content edge. */
	.page-head {
		display: flex; align-items: center; justify-content: space-between;
		gap: var(--space-4);
		margin-bottom: var(--space-5);
	}
	.page-head h2.page-title { margin: 0; }

	.error { margin: 0 0 var(--space-4); padding: 0 var(--space-2); font-size: var(--text-footnote); color: var(--red-text); }

	.loading {
		margin: 0; padding: 0 var(--space-2);
		font-size: var(--text-subhead);
		color: var(--text-tertiary);
	}

	.empty {
		display: flex; flex-direction: column; align-items: center; gap: var(--space-2);
		padding: var(--space-8) var(--space-6);
	}
	.empty-title { font-size: var(--text-subhead); color: var(--text-secondary); }
	.empty-hint { margin-bottom: var(--space-2); font-size: var(--text-caption); color: var(--text-tertiary); }
</style>
