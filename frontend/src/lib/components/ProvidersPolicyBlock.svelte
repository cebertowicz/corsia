<script lang="ts">
	import Toggle from './ui/Toggle.svelte';
	import Button from './ui/Button.svelte';
	import IconButton from './ui/IconButton.svelte';
	import AccountUsageRow from './v7/AccountUsageRow.svelte';
	import { loadProviderPolicy, saveProviderPolicy, saveUseWorkspace, type ProviderEntry, type ProviderPolicy } from '$lib/api/providerPolicy';
	import { loadVendorAccounts, vendorAccounts } from '$lib/stores/vendorAccounts';
	import CloudModelsOffSetting from './CloudModelsOffSetting.svelte';
	import { requestSettingsSection, settingsAnchor } from '$lib/stores/settingsNavigation';

	let { projectId, onSignIn }: { projectId?: string; onSignIn?: (provider: string) => void } = $props();
	let policy = $state<ProviderPolicy | null>(null);
	let error = $state('');
	let accountsError = $state('');
	let loading = $state(true);
	let busy = $state(false);
	const accountProviders = $derived(policy?.providers?.filter((provider) => provider.capabilities.units === 'accounts') ?? []);

	$effect(() => { void load(); });
	async function load() {
		loading = true; error = ''; accountsError = '';
		const [result, accounts] = await Promise.all([loadProviderPolicy(projectId), loadVendorAccounts()]);
		if (result.ok && Array.isArray(result.value?.providers)) policy = result.value;
		else error = result.ok ? 'Could not load providers.' : result.error;
		if (!accounts.ok) accountsError = accounts.error ?? 'Could not load accounts.';
		loading = false;
	}
	async function toggleScope(useWorkspace: boolean) {
		if (!projectId || !policy || busy) return;
		busy = true;
		try {
			const result = await saveUseWorkspace(projectId, useWorkspace, policy.override_revision ?? 0);
			if (result.ok) policy = result.value; else error = result.error;
		} finally {
			busy = false;
		}
	}
	async function save(entry: ProviderEntry, patch: Partial<ProviderEntry>) {
		if (busy) return;
		busy = true; error = '';
		try {
			const result = await saveProviderPolicy({ ...entry, ...patch }, projectId);
			if (result.ok && policy) policy = { ...policy, providers: policy.providers.map((item) => item.provider_key === entry.provider_key ? result.value : item) };
			else if (!result.ok) error = result.error;
		} finally {
			busy = false;
		}
	}
	function providerName(provider: ProviderEntry) {
		return provider.provider_key === 'claude-code' ? 'Claude Code' : 'Codex';
	}
	function accountFor(unitId: string) {
		return $vendorAccounts.find((account) => account.id === unitId);
	}
	function overrideSummary() {
		const projects = policy?.overriding_projects ?? [];
		if (!projects.length) return 'Every project uses these settings';
		return `Overridden in ${projects.length} ${projects.length === 1 ? 'project' : 'projects'}: ${projects.map((project) => project.name).join(', ')}`;
	}
	// The workspace's order sits under Providers, above every project's override.
	function openWorkspaceProviders() {
		requestSettingsSection('providers', 'workspace-provider-policy');
	}
	function setEnabled(provider: ProviderEntry, unitId: string, enabled: boolean) {
		void save(provider, { disabled: enabled ? provider.disabled.filter((id) => id !== unitId) : [...provider.disabled, unitId] });
	}
	function move(entry: ProviderEntry, unitId: string, direction: -1 | 1) {
		const order = [...(entry.order ?? entry.units.map((unit) => unit.id))];
		const index = order.indexOf(unitId);
		const destination = index + direction;
		if (index < 0 || destination < 0 || destination >= order.length) return;
		[order[index], order[destination]] = [order[destination], order[index]];
		void save(entry, { order });
	}
</script>

<section class="providers-policy" aria-label={projectId ? 'Project providers' : 'Workspace providers'} data-settings-anchor={projectId ? 'project-provider-policy' : 'workspace-provider-policy'} use:settingsAnchor={projectId ? 'project-provider-policy' : 'workspace-provider-policy'}>
	{#if projectId}
		<h2>Models</h2>
		<CloudModelsOffSetting {projectId} />
	{/if}
	<h2>Accounts and rollover</h2>
	{#if loading}<p role="status">Loading providers…</p>
	{:else if error}<p class="error" role="alert">{error}</p><Button size="sm" onclick={load}>Reload providers</Button>
	{:else if policy}
		{#if accountsError}<p class="error" role="alert">{accountsError}</p><Button size="sm" onclick={load}>Reload providers</Button>{/if}
		{#if projectId}
			<div class="scope-setting">
				<div>
					<span class="scope-label">Use workspace settings</span>
					{#if policy.use_workspace ?? true}
						<p>Use the workspace's provider order and rollover settings for this project.</p>
					{:else}
						<p>Turning this on replaces this project's settings with the workspace's. Turning it off again starts from the workspace values.</p>
					{/if}
					<button class="workspace-link" type="button" onclick={openWorkspaceProviders}>Open workspace providers</button>
				</div>
				<Toggle checked={policy.use_workspace ?? true} disabled={busy} ariaLabel="Use workspace settings" onchange={toggleScope} />
			</div>
		{/if}
		{#if !projectId || !policy.use_workspace}
			{#each accountProviders as provider (provider.provider_key)}
				<div class="provider">
					<h3>{providerName(provider)}</h3>
					{#if !projectId}<p>{overrideSummary()}</p>{/if}
					{#if provider.units.length === 0}
						<p>No {providerName(provider)} account signed in</p>
						{#if onSignIn}<Button size="sm" onclick={() => onSignIn?.(provider.provider_key)}>Sign in</Button>{/if}
					{:else if provider.units.length === 1}
						{@const unit = provider.units[0]}
						{#if accountFor(unit.id)}
							<AccountUsageRow account={accountFor(unit.id)!} compact>
								<Toggle checked={unit.enabled} disabled={busy} ariaLabel={unit.label} onchange={(value) => setEnabled(provider, unit.id, value)} />
							</AccountUsageRow>
						{:else}
							<div class="account-row"><span>{unit.label}</span><Toggle checked={unit.enabled} disabled={busy} ariaLabel={unit.label} onchange={(value) => setEnabled(provider, unit.id, value)} /></div>
						{/if}
					{:else}
						{#if provider.capabilities.rollover}
							<div class="line"><span>Roll over when an account hits its limit</span><Toggle checked={provider.rollover} disabled={busy} ariaLabel="Roll over when an account hits its limit" onchange={(value) => save(provider, { rollover: value })} /></div>
						{/if}
						{#each provider.units as unit, index (unit.id)}
							{#if accountFor(unit.id)}
								<AccountUsageRow account={accountFor(unit.id)!} compact order={provider.rollover ? index + 1 : null}>
									{#if provider.rollover}
										<span class="order-controls" aria-label={`${unit.label} rollover order`}>
											<IconButton size="sm" ariaLabel={`Move ${unit.label} up`} disabled={busy || index === 0} onclick={() => move(provider, unit.id, -1)}><svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6" /></svg></IconButton>
											<IconButton size="sm" ariaLabel={`Move ${unit.label} down`} disabled={busy || index === provider.units.length - 1} onclick={() => move(provider, unit.id, 1)}><svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6" /></svg></IconButton>
										</span>
									{/if}
									<Toggle checked={unit.enabled} disabled={busy} ariaLabel={unit.label} onchange={(value) => setEnabled(provider, unit.id, value)} />
								</AccountUsageRow>
							{:else}
								<div class="account-row"><span>{unit.label}</span><Toggle checked={unit.enabled} disabled={busy} ariaLabel={unit.label} onchange={(value) => setEnabled(provider, unit.id, value)} /></div>
							{/if}
						{/each}
					{/if}
				</div>
			{/each}
		{/if}
	{/if}
</section>

<style>
	.providers-policy { display: flex; flex-direction: column; gap: var(--space-4); }
	h2 { margin: 0; font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary); }
	h3 { margin: 0; font-size: var(--text-subhead); }
	.provider { display: flex; flex-direction: column; gap: 10px; }
	.scope-setting, .line, .account-row, .order-controls { display: flex; align-items: center; gap: 8px; }
	.scope-setting, .line, .account-row { justify-content: space-between; }
	.order-controls :global(.ico-btn) { width: 24px; height: 24px; }
	.scope-setting > div { min-width: 0; }
	/* A narrow project policy line must wrap its copy, never shrink the switch track. */
	.scope-setting :global(.tg) { flex: 0 0 auto; }
	.scope-label, .line > span { color: var(--text-primary); font-size: var(--text-subhead); font-weight: 500; }
	.workspace-link { appearance: none; border: 0; padding: 0; background: transparent; color: var(--accent-text); cursor: pointer; font: inherit; font-size: var(--text-footnote); text-decoration: underline; }
	p { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.error { color: var(--red-text); }
</style>
