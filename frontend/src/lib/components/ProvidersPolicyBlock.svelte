<script lang="ts">
	import Toggle from './ui/Toggle.svelte';
	import Button from './ui/Button.svelte';
	import ProviderAccountPolicyControls from './v7/ProviderAccountPolicyControls.svelte';
	import AccountUsageRow from './v7/AccountUsageRow.svelte';
	import type { ProviderEntry } from '$lib/api/providerPolicy';
	import { createProviderPolicyState } from '$lib/stores/providerPolicyState.svelte';
	import { loadVendorAccounts, vendorAccounts } from '$lib/stores/vendorAccounts';
	import { requestSettingsSection, settingsAnchor } from '$lib/stores/settingsNavigation';

	const { projectId, onSignIn }: { projectId: string; onSignIn?: (provider: string) => void } = $props();
	const policyState = createProviderPolicyState(() => projectId);
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
	// Project policy stays with its project; workspace account policy lives in the provider Accounts tab.
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
		<div class="scope-setting">
				<div>
					<span class="scope-label">Use workspace settings</span>
					<p>Use the workspace's account order and rollover.</p>
					<span class="workspace-link"><Button variant="ghost" size="sm" onclick={openWorkspaceProviders}>Open workspace providers</Button></span>
				</div>
				<Toggle checked={policy.use_workspace ?? true} disabled={busy} ariaLabel="Use workspace settings" onchange={toggleScope} />
			</div>
		{#if !policy.use_workspace}
			{#each accountProviders as provider (provider.provider_key)}
				<div class="provider">
					<h3>{providerName(provider)}</h3>
					{#if provider.units.length === 0}
						<p>No {providerName(provider)} account signed in</p>
						{#if onSignIn}<Button size="sm" variant="ghost" onclick={() => onSignIn?.(provider.provider_key)}>Sign in</Button>{/if}
					{:else if provider.units.length === 1}
						{@const unit = provider.units[0]}
						{#if accountFor(unit.id)}
							<AccountUsageRow account={accountFor(unit.id)!} compact>
								<ProviderAccountPolicyControls accountName={unit.label} enabled={unit.enabled} disabled={busy} ontoggle={(value) => setEnabled(provider, unit.id, value)} />
							</AccountUsageRow>
						{:else}
							<div class="account-row"><span>{unit.label}</span><ProviderAccountPolicyControls accountName={unit.label} enabled={unit.enabled} disabled={busy} ontoggle={(value) => setEnabled(provider, unit.id, value)} /></div>
						{/if}
					{:else}
						{#if provider.capabilities.rollover}
							<div class="line"><span>Roll over when an account hits its limit</span><Toggle checked={provider.rollover} disabled={busy} ariaLabel="Roll over when an account hits its limit" onchange={(value) => save(provider, { rollover: value })} /></div>
						{/if}
						{#each provider.units as unit, index (unit.id)}
							{#if accountFor(unit.id)}
								<AccountUsageRow account={accountFor(unit.id)!} compact order={provider.rollover ? index + 1 : null}>
									<ProviderAccountPolicyControls
										accountName={unit.label}
										enabled={unit.enabled}
										disabled={busy}
										position={provider.rollover ? index : null}
										total={provider.units.length}
										onmove={(direction) => move(provider, unit.id, direction)}
										ontoggle={(value) => setEnabled(provider, unit.id, value)}
									/>
								</AccountUsageRow>
							{:else}
								<div class="account-row"><span>{unit.label}</span><ProviderAccountPolicyControls accountName={unit.label} enabled={unit.enabled} disabled={busy} ontoggle={(value) => setEnabled(provider, unit.id, value)} /></div>
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
	/* The project policy is the section; provider names are its subordinate group labels. */
	h2 { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-primary); }
	h3 { margin: 0; font-size: var(--text-caption); font-weight: var(--fw-regular, 400); color: var(--text-tertiary); }
	.provider { display: flex; flex-direction: column; gap: var(--space-1); }
	.provider > p { margin-bottom: var(--space-1); }
	.scope-setting, .line, .account-row { display: flex; align-items: center; gap: var(--space-2); }
	.line, .account-row { min-height: var(--list-row-min-height); }
	.account-row > span { font-size: var(--text-subhead); color: var(--text-primary); }
	.scope-setting, .line, .account-row { justify-content: space-between; }
	.scope-setting > div { min-width: 0; }
	/* A narrow project policy line must wrap its copy, never shrink the switch track. */
	.scope-setting :global(.tg) { flex: 0 0 auto; }
	.scope-label, .line > span { color: var(--text-primary); font-size: var(--text-subhead); }
	/* Going to another Settings page is a text action on the same content column. */
	.workspace-link { display: block; margin-top: var(--space-1); }
	.workspace-link :global(.btn.ghost) { padding-inline: 0; }
	p { margin: 0; color: var(--text-tertiary); font-size: var(--text-caption); }
	.error { color: var(--red-text); }
</style>
