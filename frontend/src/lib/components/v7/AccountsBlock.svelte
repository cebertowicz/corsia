<script lang="ts">
	import { onMount, tick } from 'svelte';
	import './AccountsBlock.css';
	import { Button, Field, Message } from '$lib/components/ui';
	import Toggle from '../ui/Toggle.svelte';
	import Menu, { type MenuItem } from '../ui/Menu.svelte';
	import { createRowMenu } from '../ui/rowMenu.svelte';
	import ConfirmDialog from '../ConfirmDialog.svelte';
	import OverageSwitch from './OverageSwitch.svelte';
	import AccountUsageRow from './AccountUsageRow.svelte';
	import AccountBillingRow from './AccountBillingRow.svelte';
	import ProviderAccountPolicyControls from './ProviderAccountPolicyControls.svelte';
	import PolicySaveStatus from './PolicySaveStatus.svelte';
	import { signinMessage } from './accountSigninMessage';
	import { VENDORS } from '$lib/conductorSources';
	import { on } from '$lib/ws/events';
	import type { HostProvider } from '$lib/stores/providersV7';
	import { createProviderPolicyState } from '$lib/stores/providerPolicyState.svelte';
	import { openProjectSettings } from '$lib/stores/settingsNavigation';
	import {
		addVendorAccount,
		cancelVendorSignin,
		checkVendorSignin,
		fetchRemovedVendorAccounts,
		loadVendorAccounts,
		removeVendorAccount,
		renameVendorAccount,
		signOutVendorAccount,
		startVendorSignin,
		vendorAccounts,
		type SigninResult,
		type VendorAccount,
	} from '$lib/stores/vendorAccounts';

	interface Props { provider: HostProvider; }
	const { provider }: Props = $props();

	let actionError = $state('');
	let refreshError = $state('');
	let adding = $state(false);
	let addName = $state('');
	let pending = $state<Record<string, boolean>>({});
	const addBusy = $derived(!!pending.add);
	let editingId = $state<string | null>(null);
	let editName = $state('');
	let editError = $state('');
	let removeTarget = $state<VendorAccount | null>(null);
	const accountMenu = createRowMenu<VendorAccount>((account) => account.state !== 'removed' && !pending[account.id]);
	const menuItems = $derived.by<MenuItem[]>(() => {
		const account = accountMenu.open?.item;
		if (!account) return [];
		const disabled = account.details_stale || !!pending[account.id];
		return [
			{ id: 'rename', label: 'Rename', disabled },
			...(account.state === 'legacy' ? [] : [
				{ id: 'sign-out', label: 'Sign out', disabled },
				{ id: 'remove', label: 'Remove', danger: true, disabled },
			]),
		];
	});
	const policyState = createProviderPolicyState();
	const policyError = $derived(policyState.error);
	const policyProvider = $derived(policyState.policy?.providers.find((entry) => entry.provider_key === provider.name));
	const rolloverTarget = $derived(`rollover:${provider.name}`);
	const overridingProjects = $derived(policyState.policy?.overriding_projects ?? []);
	let signin = $state<Record<string, SigninResult>>({});

	// The historical view owns its removed accounts, so a later plain reload of the shared list cannot strand it.
	let historicalAccounts = $state<VendorAccount[] | null>(null);
	const showHistorical = $derived(historicalAccounts !== null);

	const removedAccounts = $derived((historicalAccounts ?? []).filter((account) => account.provider === provider.name));
	const accounts = $derived([...$vendorAccounts.filter((account) => account.provider === provider.name && account.state !== 'removed' && !removedAccounts.some((removed) => removed.id === account.id)), ...removedAccounts]);
	const showRows = $derived(accounts.length > 0 || Object.keys(signin).length > 0);
	const rolloverActive = $derived(!!policyProvider?.rollover && policyProvider.units.length >= 2);
	// The saved rollover order is the order of `units`: numbered rows follow it, and accounts with no unit (removed ones) follow, unnumbered.
	const rows = $derived.by(() => {
		const byId = new Map(accounts.filter((account) => account.state !== 'removed').map((account) => [account.id, account]));
		const ordered = (policyProvider?.units ?? []).flatMap((unit, position) => byId.has(unit.id) ? [{ account: byId.get(unit.id)!, unit, position }] : []);
		const numbered = new Set(ordered.map((row) => row.account.id));
		return [...ordered, ...accounts.filter((account) => !numbered.has(account.id)).map((account) => ({ account, unit: undefined, position: -1 }))];
	});

	// The policy lists exactly this vendor's accounts, so it reloads whenever that set changes.
	const accountIdsKey = $derived($vendorAccounts.filter((account) => account.provider === provider.name && account.state !== 'removed').map((account) => account.id).sort().join('\n'));
	let policyAccountIds: string | null = null;
	$effect(() => {
		const key = accountIdsKey;
		if (policyAccountIds === null || key === policyAccountIds) return;
		policyAccountIds = key;
		void policyState.load();
	});

	// The first read of a new login's models ends with models_updated: the list then shows whether the account has any.
	const refreshAccounts = async () => { const result = await loadVendorAccounts(); if (!result.superseded) refreshError = result.ok ? '' : `${result.error ?? 'Couldn’t load your accounts.'}${result.ref ? ` Reference: ${result.ref}` : ''}`; };
	onMount(() => on('models_updated', refreshAccounts));

	onMount(async () => {
		void policyState.load();
		const result = await loadVendorAccounts();
		policyAccountIds = accountIdsKey;
		if (!result.ok) {
			if (!result.superseded) actionError = result.error ?? 'Couldn’t load your accounts.';
			return;
		}
		for (const account of accounts) {
			if (account.state !== 'signed_out') continue;
			const current = await checkVendorSignin(account.id);
			if (current.ok && current.signin) {
				rememberSignin(current.signin);
				if (current.error) actionError = current.error;
			} else if (!current.absent) actionError = current.error ?? 'Could not check sign-in.';
		}
	});

	function beginAction(id: string): boolean {
		if (pending[id]) return false;
		pending = { ...pending, [id]: true };
		return true;
	}

	function endAction(id: string) { pending = { ...pending, [id]: false }; }

	function accountName(id: string | null | undefined): string | null {
		const account = accounts.find((candidate) => candidate.id === id);
		return account ? displayName(account) : null;
	}

	function stateLabel(account: VendorAccount): string | null {
		if (account.state === 'signed_out') return 'Signed out';
		if (account.state === 'legacy') return 'Managed by provider';
		if (account.state === 'removed') return 'Removed';
		const vendor = VENDORS[account.provider];
		if (account.no_usable_model && vendor) return `This ${vendor.name} account has no models for ${vendor.cli}. Sign in with another account, or turn this account off.`;
		return null;
	}

	async function focusHolder(result: SigninResult) {
		if (!result.held_by) return;
		await tick();
		document.getElementById(`vendor-account-${result.held_by}`)?.focus();
	}

	function rememberSignin(result: SigninResult) {
		signin = { ...signin, [result.account_id]: result };
		if (result.status === 'refused') void focusHolder(result);
	}

	function clearSignin(accountId: string) {
		const { [accountId]: _, ...rest } = signin;
		signin = rest;
	}

	async function openHistoricalAccounts() {
		const result = await fetchRemovedVendorAccounts();
		if (!result.ok) { actionError = result.error; return; }
		historicalAccounts = result.accounts;
	}

	async function addAccount() {
		const name = addName.trim();
		if (!name || !beginAction('add')) return;
		actionError = '';
		const added = await addVendorAccount(provider.name, name);
		endAction('add');
		if (!added.ok || !added.account) {
			actionError = added.error ?? 'Could not add account.';
			return;
		}
		adding = false; addName = '';
		await startSignin(added.account.id);
	}

	async function startSignin(accountId: string) {
		if (!beginAction(accountId)) return;
		actionError = '';
		const result = await startVendorSignin(accountId);
		endAction(accountId);
		if (!result.ok || !result.signin) {
			actionError = result.error ?? 'Could not start sign-in.';
			return;
		}
		rememberSignin(result.signin);
		if (result.error) actionError = result.error;
	}

	async function checkSignin(accountId: string) {
		if (!beginAction(accountId)) return;
		actionError = '';
		const result = await checkVendorSignin(accountId);
		endAction(accountId);
		if (!result.ok || !result.signin) {
			actionError = result.error ?? 'Could not check sign-in.';
			return;
		}
		rememberSignin(result.signin);
		if (result.error) actionError = result.error;
	}

	async function cancelSignin(accountId: string) {
		if (!beginAction(accountId)) return;
		actionError = '';
		const result = await cancelVendorSignin(accountId);
		endAction(accountId);
		if (!result.ok || !result.signin) {
			actionError = result.error ?? 'Could not cancel sign-in.';
			return;
		}
		rememberSignin(result.signin);
		if (result.error) actionError = result.error;
	}

	function displayName(account: VendorAccount): string {
		return account.label.text;
	}

	function beginRename(account: VendorAccount) {
		if (account.details_stale || pending[account.id] || (editingId && pending[editingId])) return;
		accountMenu.close(); editingId = account.id;
		editName = account.label.custom ?? '';
		editError = '';
	}

	async function saveRename(account: VendorAccount) {
		const name = editName.trim();
		if (!name || !beginAction(account.id)) return;
		editError = '';
		const result = await renameVendorAccount(account, name);
		endAction(account.id);
		if (!result.ok) { editError = result.error ?? 'Could not rename account.'; return; }
		editingId = null;
	}

	async function reloadAccount(accountId: string) {
		if (!beginAction(accountId)) return;
		const result = await loadVendorAccounts();
		endAction(accountId);
		if (editingId === accountId) editError = result.ok ? '' : result.error ?? 'Couldn’t load your accounts.';
		else actionError = result.ok ? '' : result.error ?? 'Couldn’t load your accounts.';
	}

	async function signOut(account: VendorAccount) {
		if (!beginAction(account.id)) return;
		accountMenu.close(); actionError = '';
		const result = await signOutVendorAccount(account);
		endAction(account.id);
		if (!result.ok) { actionError = result.error ?? 'Could not sign out.'; return; }
		clearSignin(account.id);
		if (result.error) actionError = result.error;
	}

	async function removeAccount() {
		if (!removeTarget) return;
		const account = removeTarget;
		if (!beginAction(account.id)) return;
		removeTarget = null; accountMenu.close(); actionError = '';
		const result = await removeVendorAccount(account);
		endAction(account.id);
		if (!result.ok) { actionError = result.error ?? 'Could not remove account.'; return; }
		clearSignin(account.id);
		if (result.error) actionError = result.error;
		if (showHistorical) await openHistoricalAccounts();
	}

	function selectAccountMenu(id: string) {
		const account = accountMenu.open?.item;
		if (!account) return;
		if (id === 'rename') beginRename(account);
		else if (id === 'sign-out') void signOut(account);
		else if (id === 'remove') { accountMenu.close(); removeTarget = account; }
	}
</script>

<div class="account-manager">
{#if !showRows && accounts.length === 1}<OverageSwitch account={accounts[0]} />{/if}
{#if actionError || policyError}<Message variant="err">{actionError || policyError}</Message>{/if}
{#if policyError && !actionError}<Button size="sm" onclick={() => policyState.load()}>Retry</Button>{/if}
{#if refreshError}<Message variant="err">{refreshError}</Message><Button size="sm" onclick={refreshAccounts}>Retry</Button>{/if}
<div class="accounts" aria-label={`${provider.label} accounts`}>
	{#if !showRows}
		<div class="empty-row">
			<span>No {provider.label} accounts signed in</span>
			<Button size="sm" variant="ghost" disabled={addBusy} onclick={() => { if (!addBusy) { adding = !adding; actionError = ''; } }}>Add account</Button>
		</div>
	{:else}
		{#if policyProvider && policyProvider.units.length >= 2 && policyProvider.capabilities.rollover}
			<div class="rollover-row"><span>Roll over when an account hits its limit</span><span class="policy-control"><Toggle checked={policyProvider.rollover} disabled={policyState.isPending(rolloverTarget)} ariaLabel="Roll over when an account hits its limit" describedBy={policyState.isPending(rolloverTarget) || policyState.hasFailure(rolloverTarget) ? 'rollover-policy-status' : undefined} onchange={(value) => policyState.save(policyProvider, { rollover: value }, rolloverTarget)} /><PolicySaveStatus id="rollover-policy-status" pending={policyState.isPending(rolloverTarget)} failure={policyState.failureText(rolloverTarget)} /></span></div>
		{/if}
		{#each rows as { account, unit, position } (account.id)}
			{@const unitTarget = `unit:${provider.name}:${account.id}`}
			{@const signIn = signin[account.id]}
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<section class="account" class:ordered={rolloverActive} id={`vendor-account-${account.id}`} tabindex="0" {...accountMenu.rowHandlers(account)}>
				{#if editingId === account.id && account.state !== 'removed'}
					<Field label="Name" error={editError}>
						{#snippet trailing()}
							<div class="inline-actions">
								{#if account.details_stale}<Button size="sm" disabled={!!pending[account.id]} onclick={() => reloadAccount(account.id)}>Reload</Button>{/if}
								<Button size="sm" variant="primary" disabled={!!pending[account.id] || account.details_stale || !editName.trim()} onclick={() => saveRename(account)}>Save</Button>
								<Button size="sm" disabled={!!pending[account.id]} onclick={() => { if (!pending[account.id]) { editingId = null; editError = ''; } }}>Cancel</Button>
							</div>
						{/snippet}
						<input aria-label={`Rename ${displayName(account)}`} placeholder={displayName(account)} readonly={!!pending[account.id]} bind:value={editName} onkeydown={(event) => {
							if (event.key === 'Enter' || event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); }
							if (event.key === 'Enter' && !account.details_stale) void saveRename(account);
							if (event.key === 'Escape' && !pending[account.id]) { editingId = null; editError = ''; }
						}} />
					</Field>
				{:else}
					<div class="account-main">
						<div class="account-copy">
							<AccountUsageRow {account} compact marker={account.is_default ? 'Workspace account' : null} order={rolloverActive && unit ? position + 1 : null}>
								{#if unit}
									<ProviderAccountPolicyControls
										accountName={displayName(account)}
										enabled={unit.enabled}
										toggleDisabled={policyState.isPending(unitTarget)}
										moveDisabled={policyState.isPending(`order:${provider.name}:${account.id}`)}
										pending={policyState.isPending(unitTarget) || policyState.isPending(`order:${provider.name}:${account.id}`)}
										failure={policyState.failureText(unitTarget) ?? policyState.failureText(`order:${provider.name}:${account.id}`)}
										position={rolloverActive ? position : null}
										total={policyProvider?.units.length ?? 0}
										onmove={(direction) => policyState.move(policyProvider!, account.id, direction)}
										ontoggle={(value) => policyState.setEnabled(policyProvider!, account.id, value)}
									/>
								{/if}
							</AccountUsageRow>
							{#if stateLabel(account)}<div class="account-meta"><span>{stateLabel(account)}</span></div>{/if}
						</div>
						<!-- Row actions open with right-click, long-press, or the context-menu key. -->
					</div>
					{#if account.state === 'signed_out' && (!signIn || signIn.status === 'refused')}
						<Button size="sm" variant="ghost" loading={!!pending[account.id]} onclick={() => startSignin(account.id)}>Sign in</Button>
					{/if}
					{#if account.state !== 'removed'}<OverageSwitch {account} inline />{/if}
					<AccountBillingRow {account} />
					<!-- The shared Menu below owns positioning, focus, and escape. -->
				{/if}
				{#if account.state !== 'removed' && account.details_stale && editingId !== account.id}
					<div class="signin-state"><span>Account details need to be reloaded.</span><Button size="sm" disabled={!!pending[account.id]} onclick={() => reloadAccount(account.id)}>Reload</Button></div>
				{/if}
				{#if account.state !== 'removed' && signIn && !(signIn.status === 'completed' && account.state === 'signed_in')}
					<div class="signin-state" role="status">
						<span>{signinMessage(signIn, accountName, signin, VENDORS[account.provider]?.cli ?? null)}</span>
						{#if signIn.status === 'waiting'}
							<div class="signin-actions">
								<Button size="sm" disabled={!!pending[account.id]} onclick={() => checkSignin(account.id)}>Check</Button>
								<Button size="sm" variant="secondary" disabled={!!pending[account.id]} onclick={() => cancelSignin(account.id)}>Cancel</Button>
							</div>
						{:else if signIn.status === 'failed' || signIn.status === 'cancelled' || signIn.status === 'unavailable' || signIn.status === 'superseded'}
							<Button size="sm" variant="secondary" disabled={!!pending[account.id]} onclick={() => startSignin(account.id)}>Sign in again</Button>
						{/if}
					</div>
				{/if}
			</section>
		{/each}
	{/if}

	{#if overridingProjects.length}
		<p class="override-summary">Overridden in {overridingProjects.length} {overridingProjects.length === 1 ? 'project' : 'projects'}: {#each overridingProjects as project, index}{#if index > 0}{', '}{/if}<button class="project-link" onclick={() => openProjectSettings(project.id, 'project-provider-policy')}>{project.name}</button>{/each}</p>
	{/if}

	{#if showRows || !showHistorical}
		<div class="account-actions">
			{#if showRows}<Button size="sm" variant="ghost" disabled={addBusy} onclick={() => { if (!addBusy) { adding = !adding; actionError = ''; } }}>Add account</Button>{/if}
			{#if !showHistorical}<Button size="sm" variant="ghost" onclick={openHistoricalAccounts}>Open historical accounts</Button>{/if}
		</div>
	{/if}
	{#if adding}
		<Field label="Account name" error={actionError}>
			{#snippet trailing()}
				<div class="inline-actions">
					<Button size="sm" variant="primary" loading={addBusy} disabled={!addName.trim()} onclick={addAccount}>Add</Button>
					<Button size="sm" disabled={addBusy} onclick={() => { if (!addBusy) { adding = false; addName = ''; } }}>Cancel</Button>
				</div>
			{/snippet}
			<input aria-label={`Name for new ${provider.label} account`} readonly={addBusy} bind:value={addName} onkeydown={(event) => {
				if (event.key === 'Enter' || event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); }
				if (event.key === 'Enter') void addAccount();
				if (event.key === 'Escape' && !addBusy) { adding = false; addName = ''; }
			}} />
		</Field>
	{/if}
</div>

{#if accountMenu.open}
	<Menu ariaLabel={`Actions for ${displayName(accountMenu.open.item)}`} items={menuItems} x={accountMenu.open.x} y={accountMenu.open.y} onSelect={selectAccountMenu} onClose={accountMenu.close} />
{/if}

<ConfirmDialog
	open={!!removeTarget}
	title={`Remove ${removeTarget?.name ?? ''}?`}
	description="Threads on it hold until you choose another account."
	confirmLabel="Remove"
	destructive
	onConfirm={removeAccount}
	onCancel={() => removeTarget = null}
/>
</div>

