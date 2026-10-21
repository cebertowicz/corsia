<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Button, Field, Message } from '$lib/components/ui';
	import ConfirmDialog from '../ConfirmDialog.svelte';
	import OverageSwitch from './OverageSwitch.svelte';
	import AccountUsageRow from './AccountUsageRow.svelte';
	import AccountBillingRow from './AccountBillingRow.svelte';
	import type { HostProvider } from '$lib/stores/providersV7';
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
	let menuId = $state<string | null>(null);
	let removeTarget = $state<VendorAccount | null>(null);
	let signin = $state<Record<string, SigninResult>>({});

	// The historical view owns its removed accounts, so a later plain reload of the shared list cannot strand it.
	let historicalAccounts = $state<VendorAccount[] | null>(null);
	const showHistorical = $derived(historicalAccounts !== null);

	const removedAccounts = $derived((historicalAccounts ?? []).filter((account) => account.provider === provider.name));
	const accounts = $derived([...$vendorAccounts.filter((account) => account.provider === provider.name && account.state !== 'removed' && !removedAccounts.some((removed) => removed.id === account.id)), ...removedAccounts]);
	const showRows = $derived(accounts.length > 0 || Object.keys(signin).length > 0);

	onMount(async () => {
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

	function stateLabel(account: VendorAccount): string {
		if (account.state === 'signed_out') return 'Signed out';
		if (account.state === 'legacy') return 'Managed by provider';
		if (account.state === 'signed_in') return 'Signed in';
		if (account.state === 'removed') return 'Removed';
		return account.state;
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
		menuId = null; editingId = account.id;
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
		menuId = null; actionError = '';
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
		removeTarget = null; menuId = null; actionError = '';
		const result = await removeVendorAccount(account);
		endAction(account.id);
		if (!result.ok) { actionError = result.error ?? 'Could not remove account.'; return; }
		clearSignin(account.id);
		if (result.error) actionError = result.error;
		if (showHistorical) await openHistoricalAccounts();
	}
</script>

<div class="account-manager">
{#if !showRows && accounts.length === 1}<OverageSwitch account={accounts[0]} />{/if}
{#if actionError}<Message variant="err">{actionError}</Message>{/if}

<div class="accounts" aria-label={`${provider.label} accounts`}>
	{#if !showRows}
		<Button size="sm" variant="primary" disabled={addBusy} onclick={() => { if (!addBusy) { adding = !adding; actionError = ''; } }}>Add account</Button>
	{:else}
		<div class="account-heading">
			<span>Accounts</span>
			<Button size="sm" variant="primary" disabled={addBusy} onclick={() => { if (!addBusy) { adding = !adding; actionError = ''; } }}>Add account</Button>
		</div>
		{#each accounts as account (account.id)}
			{@const signIn = signin[account.id]}
			<section class="account" id={`vendor-account-${account.id}`} tabindex="-1" aria-label={`${displayName(account)}, ${stateLabel(account)}`}>
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
							<AccountUsageRow {account} />
							<div class="account-meta">
								<span>{stateLabel(account)}</span>
								{#if account.is_default}<span>Workspace account</span>{/if}
							</div>
						</div>
						<div class="account-actions">
							{#if account.state === 'signed_out' && (!signIn || signIn.status === 'refused')}
								<Button size="sm" variant="primary" loading={!!pending[account.id]} onclick={() => startSignin(account.id)}>Sign in</Button>
							{/if}
							{#if account.state !== 'removed'}
								<button class="more" disabled={!!pending[account.id]} aria-label={`More actions for ${displayName(account)}`} aria-expanded={menuId === account.id} onclick={() => menuId = menuId === account.id ? null : account.id}>
									<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/></svg>
								</button>
							{/if}
						</div>
					</div>
					{#if account.state !== 'removed'}<OverageSwitch {account} inline />{/if}
					<AccountBillingRow {account} />
					{#if account.state !== 'removed' && menuId === account.id}
						<div class="menu" role="menu">
							<button role="menuitem" disabled={account.details_stale || !!pending[account.id]} onclick={() => beginRename(account)}>Rename</button>
							{#if account.state !== 'legacy'}
								<button role="menuitem" disabled={account.details_stale || !!pending[account.id]} onclick={() => signOut(account)}>Sign out</button>
								<button role="menuitem" class="remove" disabled={account.details_stale || !!pending[account.id]} onclick={() => { menuId = null; removeTarget = account; }}>Remove</button>
							{/if}
						</div>
					{/if}
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

	{#if !showHistorical}<Button size="sm" variant="secondary" onclick={openHistoricalAccounts}>Open historical accounts</Button>{/if}
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
	.accounts { display: grid; gap: 8px; padding: 12px 18px; border-top: 1px solid var(--border-subtle); }
	.account-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: var(--text-secondary); font-size: var(--text-footnote); font-weight: 600; }
	.account { position: relative; padding: 10px 0; border-bottom: 1px solid var(--border-subtle); outline: none; }
	.account:focus { box-shadow: 0 0 0 3px var(--accent-wash); border-radius: var(--radius-xs); }
	.account-main { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
	.account-copy { min-width: 0; }
	/* The action cluster keeps its natural width; the name/meta column absorbs
	   the squeeze (it has min-width: 0 and breaks inside words). Without this
	   a long meta line starved the cluster and the overflow button wrapped. */
	.account-actions { flex-shrink: 0; }

	.account-meta { display: flex; flex-wrap: wrap; gap: 4px 8px; margin-top: 3px; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.account-meta span + span::before { content: '·'; margin-right: 8px; color: var(--text-tertiary); }
	.account-actions, .inline-actions, .signin-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
	.more { display: inline-grid; place-items: center; width: 28px; height: 28px; border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-secondary); background: var(--bg-secondary); cursor: pointer; }
	.more:hover { color: var(--text-primary); background: var(--bg-tertiary); }
	.menu { position: absolute; z-index: 2; right: 0; top: 42px; display: grid; min-width: 132px; padding: 4px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-primary); box-shadow: var(--glass-elevation-2); }
	.menu button { border: 0; border-radius: var(--radius-xs); padding: 7px 9px; background: transparent; color: var(--text-primary); font: inherit; font-size: var(--text-footnote); text-align: left; cursor: pointer; }
	.menu button:hover { background: var(--bg-tertiary); }
	.menu .remove { color: var(--red-text); }
	.signin-state { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; color: var(--text-secondary); font-size: var(--text-footnote); }
	.signin-state { min-width: 0; }
	/* The row keeps its desktop shape on narrow screens: the account name wraps
	   inside .account-copy (min-width: 0) and the overflow button stays on the
	   same line, right-aligned. Stacking the row pushed that lone 28px control
	   onto its own line, left-aligned, and made every row 46px taller. */
	@media (max-width: 480px) { .accounts { padding: 10px 12px; } .menu { top: auto; bottom: 4px; } }
</style>
