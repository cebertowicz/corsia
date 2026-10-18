<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Button, Field, Message } from '$lib/components/ui';
	import Toggle from '../ui/Toggle.svelte';
	import Menu, { type MenuItem } from '../ui/Menu.svelte';
	import { createRowMenu } from '../ui/rowMenu.svelte';
	import ConfirmDialog from '../ConfirmDialog.svelte';
	import OverageSwitch from './OverageSwitch.svelte';
	import AccountUsageRow from './AccountUsageRow.svelte';
	import AccountBillingRow from './AccountBillingRow.svelte';
	import ProviderAccountPolicyControls from './ProviderAccountPolicyControls.svelte';
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
	const overridingProjects = $derived(policyState.policy?.overriding_projects ?? []);
	let signin = $state<Record<string, SigninResult>>({});

	// The historical view owns its removed accounts, so a later plain reload of the shared list cannot strand it.
	let historicalAccounts = $state<VendorAccount[] | null>(null);
	const showHistorical = $derived(historicalAccounts !== null);

	const removedAccounts = $derived((historicalAccounts ?? []).filter((account) => account.provider === provider.name));
	const accounts = $derived([...$vendorAccounts.filter((account) => account.provider === provider.name && account.state !== 'removed' && !removedAccounts.some((removed) => removed.id === account.id)), ...removedAccounts]);
	const showRows = $derived(accounts.length > 0 || Object.keys(signin).length > 0);

	onMount(async () => {
		void policyState.load();
		const result = await loadVendorAccounts();
		if (!result.ok) {
			if (!result.superseded) actionError = result.error ?? 'Could not load accounts.';
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
		return null;
	}

	function readableCause(cause: string | null): string {
		if (!cause) return 'Sign-in didn’t complete.';
		if (cause === 'vendor_login_failed') return 'Sign-in didn’t complete.';
		if (cause === 'vendor_not_signed_in') return 'The vendor did not report this account as signed in.';
		if (cause === 'signin_check_failed') return 'CORSIA could not check the vendor sign-in.';
		if (cause === 'vendor_login_unavailable') return 'Sign-in is unavailable.';
		return cause.replaceAll('_', ' ');
	}

	function signinMessage(result: SigninResult): string {
		if (result.status === 'waiting') return 'Waiting for sign-in in your browser';
		if (result.status === 'completed') return 'Signed in';
		if (result.status === 'cancelled') return 'Sign-in cancelled';
		if (result.status === 'superseded') return 'Account changed before sign-in finished';
		if (result.status === 'unavailable') return 'Sign-in is unavailable';
		if (result.cause === 'same_account') {
			const name = accountName(result.duplicate_of);
			return name ? `Already signed in as ${name}` : 'This account is already signed in';
		}
		if (result.status === 'refused') {
			const name = accountName(result.held_by);
			const holder = result.held_by ? signin[result.held_by] : null;
			if (holder && holder.status !== 'waiting') return name ? `Sign-in was already in progress for ${name}` : 'Another sign-in was already in progress';
			return name ? `${name} is already signing in` : 'Another account is already signing in';
		}
		return readableCause(result.cause);
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
		if (editingId === accountId) editError = result.ok ? '' : result.error ?? 'Could not load accounts.';
		else actionError = result.ok ? '' : result.error ?? 'Could not load accounts.';
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

<div class="accounts" aria-label={`${provider.label} accounts`}>
	{#if !showRows}
		<div class="empty-row">
			<span>No {provider.label} accounts signed in</span>
			<Button size="sm" variant="ghost" disabled={addBusy} onclick={() => { if (!addBusy) { adding = !adding; actionError = ''; } }}>Add account</Button>
		</div>
	{:else}
		{#if policyProvider && policyProvider.units.length >= 2 && policyProvider.capabilities.rollover}
			<div class="rollover-row"><span>Roll over when an account hits its limit</span><Toggle checked={policyProvider.rollover} disabled={policyState.busy} ariaLabel="Roll over when an account hits its limit" onchange={(value) => policyState.save(policyProvider, { rollover: value })} /></div>
		{/if}
		{#each accounts as account, index (account.id)}
			{@const unit = policyProvider?.units.find((candidate) => candidate.id === account.id)}
			{@const signIn = signin[account.id]}
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<section class="account" id={`vendor-account-${account.id}`} tabindex="0" {...accountMenu.rowHandlers(account)}>
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
							<AccountUsageRow {account} compact marker={account.is_default ? 'Workspace account' : null} order={policyProvider?.rollover && policyProvider.units.length >= 2 ? index + 1 : null}>
								{#if unit}
									<ProviderAccountPolicyControls
										accountName={displayName(account)}
										enabled={unit.enabled}
										disabled={policyState.busy}
										position={policyProvider?.rollover && policyProvider.units.length >= 2 ? index : null}
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
						<span>{signinMessage(signIn)}</span>
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

<style>
	.account-manager { display: contents; }
	.account-manager :global(.field .err), .account-manager :global(.msg.err), .account-manager :global(.btn.danger) { color: var(--red-text); }
	.account-manager :global(.btn.ghost) { color: var(--text-secondary); }
	.account-manager :global(.btn.primary:hover:not(:disabled)) { background: var(--accent-wash); }
	.accounts { display: grid; gap: var(--space-3); padding: var(--space-3) var(--space-4); }
	.empty-row, .account-actions { display: flex; align-items: center; gap: var(--space-2); min-height: var(--list-row-min-height); }
	.empty-row { justify-content: space-between; color: var(--text-secondary); font-size: var(--text-caption); }
	.account-actions { flex-wrap: wrap; }
	.rollover-row { display: flex; align-items: center; justify-content: space-between; min-height: var(--list-row-min-height); gap: var(--space-2); font-size: var(--text-subhead); color: var(--text-primary); }
	/* ProviderAccountPolicyControls owns account ordering and enablement controls. */
	.override-summary { margin: 0; font-size: var(--text-caption); color: var(--text-tertiary); }
	.project-link { padding: 0; border: 0; background: none; color: inherit; font: inherit; text-decoration: underline; cursor: pointer; }
	.account { position: relative; padding: var(--space-1) 0; outline: none; }
	.account:focus { box-shadow: 0 0 0 3px var(--accent-wash); border-radius: var(--radius-xs); }
	.account-main { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
	.account-copy { flex: 1; min-width: 0; }

	.account-meta { display: flex; flex-wrap: wrap; gap: 4px 8px; margin-top: 3px; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.inline-actions, .signin-actions { display: flex; align-items: center; gap: var(--space-1); flex-wrap: wrap; }
	/* Text actions start on the account copy column; Button retains the touch target on phones. */
	.accounts :global(.btn.ghost) { padding-inline: 0; }
	/* The shared Menu component owns row-menu styling. */
	.signin-state { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; margin-top: var(--space-2); color: var(--text-secondary); font-size: var(--text-footnote); }
	.signin-state { min-width: 0; }
	/* Account copy wraps on narrow screens while its sign-in action stays aligned. */
	@media (max-width: 480px) { .accounts { padding: var(--space-2) var(--space-3); } }
</style>
