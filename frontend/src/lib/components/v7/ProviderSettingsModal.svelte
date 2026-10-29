<script lang="ts">
	import { onMount } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { get } from 'svelte/store';
	import {
		updateProvider, deleteProvider, testProvider,
	} from '$lib/stores/providers';
	import { removeNetworkServer, testNetworkServer } from '$lib/stores/networkServers';
	import { hostGroups, loadHostGroups, type HostProvider } from '$lib/stores/providersV7';
	import { Button, Field, Message, Modal } from '$lib/components/ui';
	import ConfirmDialog from '../ConfirmDialog.svelte';
	import CostPanel from './CostPanel.svelte';
	import ExternalProviderSettings from './ExternalProviderSettings.svelte';
	import OmlxServerInfoPanel from './OmlxServerInfoPanel.svelte';

	import ProviderEndpointsTab from './ProviderEndpointsTab.svelte';
	import ProviderSettingsHeader from './ProviderSettingsHeader.svelte';
	import ProviderStatusCard from './ProviderStatusCard.svelte';
	import AccountsBlock from './AccountsBlock.svelte';
	import type { ProviderSettingsTab } from './providerSettingsTypes';

	interface Props {
		provider: HostProvider;
		initialTab?: ProviderSettingsTab;
		onClose: () => void;
	}

	const { provider: providerProp, initialTab, onClose }: Props = $props();

	const providerIndex = $derived.by((): Map<string, HostProvider> => {
		const m = new Map<string, HostProvider>();
		for (const g of $hostGroups) {
			for (const p of g.providers) {
				const key = `${p.name}|${p.server?.id ?? ''}`;
				m.set(key, p);
			}
		}
		return m;
	});
	const provider = $derived(
		providerIndex.get(`${providerProp.name}|${providerProp.server?.id ?? ''}`) ?? providerProp,
	);

	let activeTab = $state<ProviderSettingsTab>(initialTab ?? 'overview');
	onMount(() => { if (providerProp.type === 'harness') activeTab = 'accounts'; });
	let actionError = $state('');
	let actionMessage = $state('');
	let busy = $state(false);
	let testing = $state(false);
	let daemonAction = $state<'start' | 'stop' | 'reload' | null>(null);

	const hasServer = $derived(!!provider.server);

	const tabs = $derived<{ id: ProviderSettingsTab; label: string }[]>(
		provider.type === 'cloud'
			? [
				{ id: 'overview', label: 'Overview' },
				{ id: 'cost', label: 'Cost' },
				{ id: 'advanced', label: 'Advanced' },
			]
			: provider.type === 'harness'
				? [
					{ id: 'accounts', label: 'Accounts' },
					{ id: 'overview', label: 'Overview' },
				]
				: [
					{ id: 'overview', label: 'Overview' },
					{ id: 'endpoints', label: 'Endpoints' },
					{ id: 'advanced', label: 'Advanced' },
				],
	);

	let editApiBase = $state('');
	let editApiKey = $state('');
	let editApiBaseInitialized = false;

	$effect(() => {
		if (editApiBaseInitialized) return;
		editApiBase = provider.raw.api_base ?? '';
		editApiBaseInitialized = true;
	});

	function clearMessages() { actionError = ''; actionMessage = ''; }

	async function handleTest() {
		clearMessages(); busy = true; testing = true;
		const r = await testProvider(provider.name);
		busy = false; testing = false;
		if (r.ok) actionMessage = r.message ?? 'Connection OK';
		else actionError = r.error ?? 'Test failed';
	}

	async function handleTestLocal() {
		clearMessages(); busy = true; testing = true;
		if (provider.server?.kind === 'external') {
			const r = await testNetworkServer(provider.server.id);
			busy = false; testing = false;
			if (r.ok) actionMessage = r.message;
			else actionError = r.message;
			await loadHostGroups();
			return;
		}
		try {
			const t = get(token) || '';
			const res = await fetch(
				`/api/settings/providers/${provider.name}/status?token=${t}`,
			);
			if (res.ok) {
				const data = await res.json();
				const running = !!data?.server?.running;
				if (running) {
					actionMessage = data?.server?.port
						? `Reachable on port ${data.server.port}`
						: 'Reachable';
				} else {
					actionError = 'Provider is not running';
				}
				return;
			}
			if (res.status === 400 && provider.server) {
				if (provider.server.status === 'online') {
					actionMessage = provider.server.port
						? `Server reachable on port ${provider.server.port}`
						: 'Server reachable';
				} else {
					actionError = `Server is ${provider.server.status}`;
				}
				return;
			}
			const data = await res.json().catch(() => ({}));
			actionError = data.detail ?? 'Couldn’t test the connection. Try again.';
		} catch {
			actionError = 'Couldn’t test the connection. Try again.';
		} finally {
			busy = false; testing = false;
		}
	}

	// An in-app dialog, not window.confirm: an embedded web view may never show the
	// native one and answer false, so Remove silently did nothing.
	let confirmingRemove = $state(false);
	const removeDescription = $derived(
		provider.type === 'cloud'
			? "This disconnects the provider from CORSIA and removes its models from the picker. Its saved key is deleted too; you'll need to enter it again to add it back."
			: 'This disconnects the provider from CORSIA and removes its models from the picker.',
	);

	async function handleDelete() {
		confirmingRemove = false;
		clearMessages(); busy = true;
		const r: { ok: boolean; message?: string } = hasServer && provider.server
			? await removeNetworkServer(provider.server.id)
			: await deleteProvider(provider.name);
		busy = false;
		await loadHostGroups();
		// A failure stays on screen: closing here used to hide it.
		if (!r.ok) {
			actionError = `Couldn't remove ${provider.server?.instance_name || provider.label}: ${r.message ?? 'the server refused'}`;
			return;
		}
		onClose();
	}

	async function handleSaveKey() {
		clearMessages();
		const trimmed = editApiKey.trim();
		if (!trimmed) return;
		busy = true;
		const r = await updateProvider(provider.name, {
			api_key: trimmed,
			api_base: editApiBase.trim() || null,
		});
		busy = false;
		if (r.ok) {
			actionMessage = 'API key saved';
			editApiKey = '';
			await loadHostGroups();
		} else {
			actionError = r.error ?? 'Couldn’t save the key. Try again.';
		}
	}

	async function handleSaveBase() {
		clearMessages(); busy = true;
		const r = await updateProvider(provider.name, { api_base: editApiBase.trim() || null });
		busy = false;
		if (r.ok) { actionMessage = 'Base URL saved'; await loadHostGroups(); }
		else actionError = r.error ?? 'Failed to save';
	}

	// Only a managed (oMLX) daemon has start/stop/reload; an External endpoint on
	// this machine is someone else's server.
	const isLocalhostDaemon = $derived.by(() => {
		if (!provider.server || provider.server.kind === 'external') return false;
		return (provider.server.endpoints ?? []).some(
			e => e.host === '127.0.0.1' || e.host === 'localhost',
		);
	});
	const daemonRunning = $derived(provider.server?.status === 'online');

	async function handleDaemonAction(action: 'start' | 'stop' | 'reload') {
		if (!provider.server) return;
		clearMessages(); busy = true; daemonAction = action;
		const failure = action === 'reload' ? 'Couldn’t refresh the model list. Try again.'
			: `Couldn’t ${action} the model server. Try again.`;
		const success = action === 'reload' ? 'Model list refreshed.'
			: `Model server ${action === 'start' ? 'started' : 'stopped'}.`;
		try {
			const t = get(token) || '';
			const res = await fetch(
				`/api/settings/network-servers/${provider.server.id}/lifecycle/${action}?token=${t}`,
				{ method: 'POST' },
			);
			const data = await res.json().catch(() => ({}));
			if (res.ok && data.ok !== false) {
				actionMessage = data.message ?? success;
			} else {
				const reference = typeof data.ref === 'string' && data.ref ? ` Reference: ${data.ref}` : '';
				actionError = `${data.message ?? data.detail ?? failure}${reference}`;
			}
		} catch {
			actionError = failure;
		} finally {
			busy = false; daemonAction = null;
			await loadHostGroups();
		}
	}

</script>

<Modal size="md" onClose={onClose}>
	{#snippet head()}
		<ProviderSettingsHeader {provider} {tabs} bind:activeTab {onClose} />
	{/snippet}

	{#if actionError}<Message variant="err">{actionError}</Message>{/if}
	{#if actionMessage}<Message variant="ok">{actionMessage}</Message>{/if}

	{#if activeTab === 'accounts' && provider.type === 'harness'}
		<AccountsBlock {provider} />
	{:else if activeTab === 'overview'}
		<ProviderStatusCard {provider} />

		<div class="actions">
			{#if provider.type === 'cloud'}
				<Button disabled={busy} onclick={handleTest}>{testing ? 'Testing…' : 'Test connection'}</Button>
			{:else if hasServer}
				{#if isLocalhostDaemon}
					{#if daemonRunning}
						<Button disabled={busy} onclick={() => handleDaemonAction('stop')}>{daemonAction === 'stop' ? 'Stopping…' : 'Stop'}</Button>
						<Button disabled={busy} onclick={() => handleDaemonAction('reload')}>{daemonAction === 'reload' ? 'Reloading…' : 'Reload models'}</Button>
					{:else}
						<Button variant="primary" disabled={busy} onclick={() => handleDaemonAction('start')}>{daemonAction === 'start' ? 'Starting…' : 'Start'}</Button>
					{/if}
					<Button disabled={busy} onclick={handleTestLocal}>{testing ? 'Testing…' : 'Test connection'}</Button>

				{:else}
					<Button disabled={busy} onclick={handleTestLocal}>{testing ? 'Testing…' : 'Test connection'}</Button>
				{/if}
			{/if}
			{#if provider.type !== 'harness'}
				<span style="flex: 1"></span>
				<Button variant="danger" disabled={busy} onclick={() => (confirmingRemove = true)}>Remove provider…</Button>
			{/if}
		</div>
	{:else if activeTab === 'endpoints' && hasServer && provider.server}
		<ProviderEndpointsTab server={provider.server} />
	{:else if activeTab === 'cost'}
		<CostPanel providerName={provider.name} />
	{:else if activeTab === 'advanced'}
		{#if provider.type === 'cloud'}
			<Field label="API key">
				{#snippet trailing()}
					<Button size="sm" variant="primary" disabled={busy || !editApiKey.trim()} onclick={handleSaveKey}>Save</Button>
				{/snippet}
				<input type="password" placeholder={provider.raw.has_key ? '••••••••  (replace existing)' : 'Paste new API key'} bind:value={editApiKey} />
			</Field>
			<Field label="Base URL">
				{#snippet trailing()}
					<Button size="sm" disabled={busy} onclick={handleSaveBase}>Save</Button>
				{/snippet}
				<input placeholder="default endpoint" bind:value={editApiBase} />
			</Field>
		{:else if hasServer && provider.server?.kind === 'external'}
			<ExternalProviderSettings server={provider.server} />
		{:else if hasServer && provider.name === 'omlx' && provider.server}
			<OmlxServerInfoPanel serverId={provider.server.id} />
		{:else if hasServer}
			<p class="hint">{provider.label} is added manually by host:port. Its settings are changed on that server, not here.</p>
		{/if}
	{/if}
</Modal>

<ConfirmDialog
	open={confirmingRemove}
	title="Remove {provider.server?.instance_name || provider.label}?"
	description={removeDescription}
	confirmLabel="Remove"
	destructive
	onConfirm={handleDelete}
	onCancel={() => (confirmingRemove = false)}
/>

<style>
	.actions {
		display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap;
		align-items: center;
	}

	.hint {
		font-size: var(--text-subhead); color: var(--text-tertiary);
		line-height: 1.6;
		padding: 14px 16px;
		background: var(--bg-tertiary);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		margin: 10px 0;
	}

	@media (max-width: 480px) {
		.actions { gap: 6px; }
	}
</style>
