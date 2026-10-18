<script lang="ts">
	import Toggle from './ui/Toggle.svelte';
	import Button from './ui/Button.svelte';
	import IconButton from './ui/IconButton.svelte';
	import AccountUsageRow from './v7/AccountUsageRow.svelte';
	import type { ProviderEntry } from '$lib/api/providerPolicy';
	import { createProviderPolicyState, providerOverrideSummary } from '$lib/stores/providerPolicyState.svelte';
	import { loadVendorAccounts, vendorAccounts } from '$lib/stores/vendorAccounts';
	import { requestSettingsSection, settingsAnchor } from '$lib/stores/settingsNavigation';

	let { projectId, onSignIn }: { projectId?: string; onSignIn?: (provider: string) => void } = $props();
	const policyState = createProviderPolicyState(projectId);
	const policy = $derived(policyState.policy);
	const error = $derived(policyState.error);
	const loading = $derived(policyState.loading);
	const busy = $derived(policyState.busy);
	let accountsError = $state('');
	const accountProviders = $derived(policy?.providers?.filter((provider) => provider.capabilities.units === 'accounts') ?? []);

	$effect(() => { void load(); });
	async function load() {
		accountsError = '';
		const [, accounts] = await Promise.all([policyState.load(), loadVendorAccounts()]);
		if (!accounts.ok) accountsError = accounts.error ?? 'Could not load accounts.';
	}
	async function toggleScope(useWorkspace: boolean) { await policyState.toggleScope(useWorkspace); }
	async function save(entry: ProviderEntry, patch: Partial<ProviderEntry>) { await policyState.save(entry, patch); }
	function providerName(provider: ProviderEntry) {
		return provider.provider_key === 'claude-code' ? 'Claude Code' : 'Codex';
	}
	function accountFor(unitId: string) {
		return $vendorAccounts.find((account) => account.id === unitId);
	}
	function overrideSummary() { return providerOverrideSummary(policy); }
	// The workspace's order sits under Providers, above every project's override.
	function openWorkspaceProviders() {
		requestSettingsSection('providers');
	}
	function setEnabled(provider: ProviderEntry, unitId: string, enabled: boolean) { policyState.setEnabled(provider, unitId, enabled); }
	function move(entry: ProviderEntry, unitId: string, direction: -1 | 1) { policyState.move(entry, unitId, direction); }
</script>

<section class="providers-policy" aria-label="Project providers" data-settings-anchor="project-provider-policy" use:settingsAnchor={'project-provider-policy'}>
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
					<span class="workspace-link"><Button variant="ghost" size="sm" onclick={openWorkspaceProviders}>Open workspace providers</Button></span>
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
	/* The group label, then one sub-group per vendor: its name, a caption, then its rows. */
	.providers-policy { display: flex; flex-direction: column; gap: var(--space-4); }
	h2 { margin: 0; font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary); }
	h3 { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-primary); }
	.provider { display: flex; flex-direction: column; gap: var(--space-1); }
	.provider > p { margin-bottom: var(--space-1); }
	.scope-setting, .line, .account-row, .order-controls { display: flex; align-items: center; gap: var(--space-2); }
	.line, .account-row { min-height: var(--list-row-min-height); }
	.account-row > span { font-size: var(--text-subhead); color: var(--text-primary); }
	.scope-setting, .line, .account-row { justify-content: space-between; }
	.order-controls :global(.ico-btn) { width: 24px; height: 24px; }
	.scope-setting > div { min-width: 0; }
	/* A narrow project policy line must wrap its copy, never shrink the switch track. */
	.scope-setting :global(.tg) { flex: 0 0 auto; }
	.scope-label, .line > span { color: var(--text-primary); font-size: var(--text-subhead); }
	/* Going to another Settings page is a ghost action here, as in Keys; its fill starts at the row edge. */
	.workspace-link { display: block; margin: var(--space-1) 0 0 calc(-1 * var(--space-2)); }
	p { margin: 0; color: var(--text-tertiary); font-size: var(--text-caption); }
	.error { color: var(--red-text); }
</style>
