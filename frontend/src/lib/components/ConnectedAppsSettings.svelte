<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Button } from '$lib/components/ui';
	import { projects, loadProjects } from '$lib/stores/projects';
	import { clearConnectedAppRequest, requestAppKey, settingsAnchor, settingsNavigation } from '$lib/stores/settingsNavigation';
	import {
		approveConnectedApp, connectedAppStateLine, declineConnectedApp, loadConnectedAppsRows, reconnectConnectedApp, removeAccount, removeConnectedApp,
		runtimeDownloadFailureBody, signOutOfAccount, startAccountSignIn, type AccountSignOutResult, type ConnectedAccount, type ConnectedApp,
		type ConnectedAppsRow, isOnlyCorsia, needsManualAccountStep,
	} from '$lib/api/connectedApps';
	import { anyFirstSignIn, appAccount, hasSignInToShow, needsSignInAttention, SIGN_IN_LEDE } from '$lib/appSignIn';
	import AppSignInPanel from './AppSignInPanel.svelte';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import SettingsRow from './SettingsRow.svelte';
	import WhereItWorks from './WhereItWorks.svelte';
	import MailHostDetail from './MailHostDetail.svelte';
	import ConnectAppFlow from './ConnectAppFlow.svelte';
	import AccountManualNotice from './AccountManualNotice.svelte';
	import AccountClosedTitle from './AccountClosedTitle.svelte';
	import AppAccountTitle from './AppAccountTitle.svelte';
	import { on } from '$lib/ws/events';
	import { mailHostStateLine } from '$lib/api/mailHostAccounts';
	import { accountName } from '$lib/accountAddress';
	const accountAnchor = (id: string) => `connected-account-${id}`;
	const isAccount = (row: ConnectedAppsRow): row is ConnectedAccount => row.kind === 'account';
	const accountNeedsSignIn = (account: ConnectedAccount) => account.state === 'needs_sign_in' || account.state === 'signed_out';
	const isOpenApp = (app: ConnectedApp): boolean => openId === app.id || needsSignInAttention(app);
	let rows = $state<ConnectedAppsRow[]>([]);
	let loading = $state(true);
	let offline = $state(false);
	let error = $state('');
	let actionId = $state<string | null>(null);
	/** The `<app id>:<action>` presses in flight on app rows: each button spins for its own press, so a slow Reconnect never locks Remove. */
	let appBusy = $state<Set<string>>(new Set());
	const isBusy = (id: string, action: string): boolean => appBusy.has(`${id}:${action}`);
	const rowBusy = (id: string): boolean => [...appBusy].some((key) => key.startsWith(`${id}:`));
	function setBusy(id: string, action: string, on: boolean): void {
		const next = new Set(appBusy);
		if (on) next.add(`${id}:${action}`); else next.delete(`${id}:${action}`);
		appBusy = next;
	}
	let actionError = $state('');
	let errorId = $state<string | null>(null);
	let whereRow = $state<ConnectedAppsRow | null>(null);
	let whereApprove = $state(false);
	let connectAppOpen = $state(false);
	let openId = $state<string | null>(null);
	let signInHere = $state(false);
	let signedOut = $state<Set<string>>(new Set());
	let confirmingOut = $state<ConnectedAccount | null>(null);
	let confirmingAppOut = $state<string | null>(null);
	let confirmingRemove = $state<ConnectedAccount | null>(null);
	let manualStep = $state<ConnectedAccount | null>(null);
	let removedManualStep = $state<ConnectedAccount | null>(null);
	/** A hosted-app approval that returns needs-sign-in begins its existing one-shot repair path. */
	let signInAfterApprove = $state<string | null>(null);
	let linkedRowId = $state<string | null>(null);
	let copiedAdminMessageId = $state<string | null>(null);
	const stateLine = (app: ConnectedApp) => connectedAppStateLine(app, signedOut.has(app.id));
	/** The accounts a closed title must stay distinct from: every other closed one. */
	const closedPeers = $derived(rows.filter(isAccount).filter((account) => account.id !== openId));
	const isMicrosoft = (account: ConnectedAccount) => account.provider === 'microsoft';
	const microsoftCause = (account: ConnectedAccount, cause: string) => isMicrosoft(account) && account.cause === cause;
	function accountStateLine(account: ConnectedAccount): string {
		if (account.provider === 'mailhost') return mailHostStateLine(account);
		if (microsoftCause(account, 'admin_approval_required')) return 'Needs admin approval';
		if (microsoftCause(account, 'registration_misconfigured')) return 'Microsoft app needs fixing';
		if (microsoftCause(account, 'provider_unavailable')) return 'Sign-in didn’t finish';
		switch (account.state) {
			case 'connected': return 'Connected';
			case 'needs_sign_in': return 'Needs you to sign in';
			case 'signed_out': return 'Signed out';
		}
	}
	function tone(row: ConnectedAppsRow): 'quiet' | 'failed' | 'attention' {
		if (isAccount(row) && row.provider === 'mailhost') { const problem = row.mailhost?.problem ?? null; return problem === null || problem === 'checking' ? 'quiet' : problem === 'waiting' ? 'attention' : 'failed'; }
		if (isAccount(row)) return microsoftCause(row, 'provider_unavailable') || microsoftCause(row, 'registration_misconfigured') ? 'failed' : row.state === 'connected' || row.state === 'signed_out' ? 'quiet' : 'attention';
		if (row.state === 'failed' || (row.sign_in?.status === 'failed' && !row.sign_in.signed_in)) return 'failed';
		return row.state === 'working' ? 'quiet' : 'attention';
	}
	function needsPlace(app: ConnectedApp): boolean {
		return app.state === 'awaiting_approval' && app.reach.mode === 'selected' && app.reach.project_ids.length === 0 && !app.reach.outside_projects;
	}
	function placeLine(row: ConnectedAppsRow): string {
		if (row.reach.mode === 'all') return 'All projects';
		if (isAccount(row) && isOnlyCorsia(row.reach)) return 'Only CORSIA';
		const names = [
			...$projects.filter((project) => row.reach.project_ids.includes(project.id)).map((project) => project.name),
			...(row.reach.outside_projects ? ['Outside projects'] : []),
		];
		if (names.length === 0) return 'Choose where it works';
		return names.length > 2 ? `${names.slice(0, 2).join(', ')}, ${names.length - 2} more` : names.join(', ');
	}
	function replace(next: ConnectedAppsRow): void { rows = rows.map((row) => row.id === next.id ? next : row); }
	/** Lay an answer over the row it is about: a short answer (Reconnect returns only the state) never wipes the rest of the row. */
	function replaceApp(next: Partial<ConnectedApp> & { id: string }): void {
		rows = rows.map((row) => row.id === next.id && !isAccount(row) ? { ...row, ...next, kind: 'hosted_app' } : row);
	}
	function approved(next: ConnectedApp): void {
		replaceApp(next);
		if (next.state === 'needs_sign_in') signInAfterApprove = next.id;
	}
	async function load(): Promise<void> {
		loading = rows.length === 0;
		error = '';
		offline = false;
		try {
			const next = await loadConnectedAppsRows();
			rows = next.rows;
			signInHere = next.signInHere;
			// Keep the last successful rows in component state for an offline retry.
		} catch {
			if (rows.length) offline = true;
			else error = "Couldn't load connected apps.";
		} finally { loading = false; }
	}
	async function refresh(): Promise<void> {
		try {
			const next = await loadConnectedAppsRows();
			rows = next.rows;
			signInHere = next.signInHere;
			offline = false;
			// Keep the last successful rows in component state for an offline retry.
		} catch { if (rows.length) offline = true; }
	}
	async function runApp(app: ConnectedApp, action: 'approve' | 'decline' | 'reconnect' | 'remove'): Promise<void> {
		setBusy(app.id, action, true); errorId = app.id; actionError = '';
		try {
			if (action === 'approve') approved(await approveConnectedApp(app));
			else if (action === 'decline') {
				const next = await declineConnectedApp(app.id);
				if (next.removed) rows = rows.filter((row) => row.id !== app.id); else replaceApp(next);
			} else if (action === 'reconnect') { replaceApp(await reconnectConnectedApp(app.id)); void refresh(); }
			else { await removeConnectedApp(app.id); rows = rows.filter((row) => row.id !== app.id); }
		} catch (cause) { actionError = cause instanceof Error ? cause.message : "Couldn't update this app. Try again."; }
		finally { setBusy(app.id, action, false); }
	}
	async function startSignIn(account: ConnectedAccount): Promise<void> {
		actionId = account.id; errorId = account.id; actionError = '';
		const page = signInHere ? window.open('', '_blank') : null;
		try {
			const started = await startAccountSignIn(account);
			if (page) { page.opener = null; page.location.href = started.url; }
			else window.location.assign(started.url);
		} catch (cause) {
			page?.close();
			actionError = cause instanceof Error ? cause.message : "Couldn't start the sign-in. Try again.";
		} finally { actionId = null; }
	}
	function adminApprovalMessage(account: ConnectedAccount): string {
		const scopes = account.scopes ?? [];
		if (!account.client_id || !scopes.length) return '';
		return `Please approve my CORSIA app for my account, ${account.address}. It is my own Microsoft app registration, Application (client) ID ${account.client_id}, and requests delegated Microsoft Graph access to ${scopes.slice(0, -1).join(', ')}${scopes.length > 1 ? ' and ' : ''}${scopes.at(-1)}.`;
	}
	async function copyAdminMessage(account: ConnectedAccount): Promise<void> {
		const message = adminApprovalMessage(account);
		if (!message) return;
		try {
			await navigator.clipboard.writeText(message);
			copiedAdminMessageId = account.id;
		} catch {
			errorId = account.id;
			actionError = "Couldn't copy the message. Try again.";
		}
	}
	async function signOut(account: ConnectedAccount): Promise<void> {
		actionId = account.id; errorId = account.id; actionError = '';
		try {
			const result: AccountSignOutResult = await signOutOfAccount(account);
			const row = { ...result.row, kind: 'account' as const };
			replace(row); signedOut = new Set(signedOut).add(account.id);
			manualStep = needsManualAccountStep(row, result.revocation) ? row : null;
		} catch (cause) { actionError = cause instanceof Error ? cause.message : `Couldn't change this ${account.provider_display_name} account. Try again.`; }
		finally { confirmingOut = null; actionId = null; }
	}
	async function remove(account: ConnectedAccount): Promise<void> {
		actionId = account.id; errorId = account.id; actionError = '';
		try {
			const result = await removeAccount(account);
			rows = rows.filter((row) => row.id !== account.id);
			removedManualStep = needsManualAccountStep(account, result.revocation) ? account : null;
		}
		catch (cause) { actionError = cause instanceof Error ? cause.message : `Couldn't change this ${account.provider_display_name} account. Try again.`; }
		finally { confirmingRemove = null; actionId = null; }
	}
	function capabilityLabels(account: ConnectedAccount): string { return account.capabilities.map((capability) => ({ mail: 'Email', calendar: 'Calendar', files: 'Files' }[capability])).join(', '); }

	function clearLinkedFocus(): void { linkedRowId = null; }
	/** Retry and Edit server's Save close the row and its pressed button with it: focus goes to that row's header. */
	const closeRow = (id: string): void => { openId = null; void refresh(); void tick().then(() => [...document.querySelectorAll<HTMLElement>('[data-connected-account-id]')].find((item) => item.dataset.connectedAccountId === id)?.querySelector<HTMLElement>('.face.open')?.focus()); };

	/** The button a chat link lands on: an account's Sign in or Where it works…, an app waiting on approval's Approve or Choose where it works…. */
	function linkLabel(row: ConnectedAppsRow): string | null {
		if (isAccount(row)) return accountNeedsSignIn(row) ? 'Sign in' : 'Where it works…';
		if (row.state !== 'awaiting_approval') return null;
		return needsPlace(row) ? 'Choose where it works…' : 'Approve';
	}
	$effect(() => {
		const id = $settingsNavigation.connectedAppId;
		const linked = rows.find((row) => row.id === id);
		if (loading || !id || !linked) return;
		clearConnectedAppRequest();
		openId = id;
		clearLinkedFocus();
		requestAnimationFrame(() => {
			const row = document.querySelector<HTMLElement>(`[data-connected-account-id="${CSS.escape(id)}"], [data-connected-app-id="${CSS.escape(id)}"]`);
			const label = linkLabel(linked);
			const target = [...(row?.querySelectorAll<HTMLButtonElement>('button') ?? [])].find((button) => button.textContent?.trim() === label);
			if (!target || target.disabled) return;
			target.focus();
			if (document.activeElement === target) linkedRowId = id;
		});
	});
	$effect(() => { if (offline) clearLinkedFocus(); });
	onMount(() => {
		void loadProjects(); void load();
		const afterSignIn = () => { void refresh(); };
		window.addEventListener('focus', afterSignIn);
		// A mail-host check runs in the background and the host says when it ends.
		const offChecked = on('connected_apps_updated', afterSignIn);
		return () => { window.removeEventListener('focus', afterSignIn); offChecked(); };
	});
</script>

{#snippet appSecondLine(app: ConnectedApp)}{stateLine(app)}{#if !needsPlace(app)}<span class="place">{` · ${placeLine({ ...app, kind: 'hosted_app' })}`}</span>{/if}{#if app.folder}<span class="line">From Claude Code · {app.folder} folder</span>{/if}{/snippet}
<section class="connected-apps" data-settings-anchor="connected-apps" use:settingsAnchor={'connected-apps'} id="connected-apps">
	<div class="heading">
		<div>
			<p class="lede">Connect the apps you already use, like GitHub or Notion, so your agents can work in them. Each app works in the projects you choose for it.</p>
			{#if anyFirstSignIn(rows.filter((row): row is ConnectedApp => !isAccount(row)))}<p class="lede">{SIGN_IN_LEDE}</p>{/if}
		</div>
		<Button variant="primary" onclick={() => connectAppOpen = true}>Connect an app</Button>
	</div>
	{#if loading}
		<p class="state" role="status">Loading connected apps…</p>
	{:else if error}
		<div class="state" role="alert"><p>{error}</p><Button onclick={load}>Retry</Button></div>
	{:else}
		{#if offline}<div class="offline" role="status">Showing last known items. <Button variant="ghost" size="sm" onclick={load}>Retry</Button></div>{/if}
		{#if rows.length === 0}
			<p class="state">No connected apps. Connect one here, or ask CORSIA in chat to bring over the apps you use in Claude Code, Cursor or another tool.</p>
		{:else}
			<div class="rows">
				{#each rows as row (row.id)}
					{#if isAccount(row)}
						<div class="item account-item" data-connected-account-id={row.id} data-settings-anchor={accountAnchor(row.id)} use:settingsAnchor={accountAnchor(row.id)}>
							{#snippet accountTitle()}{#if openId === row.id}<span class="account-open-name">{accountName(row)}</span>{:else}<AccountClosedTitle account={row} peers={closedPeers} />{/if}{/snippet}
							<SettingsRow name={accountTitle} ariaLabel={accountName(row)} subTone={tone(row)} wrapName={openId === row.id} chevron expanded={openId === row.id} onOpen={() => openId = openId === row.id ? null : row.id}>
								{#snippet sub()}{accountStateLine(row)}<span class="place">{` · ${placeLine(row)}`}</span>{/snippet}
							</SettingsRow>
							{#if openId === row.id}
								<div class="details">
									{#if row.provider === 'mailhost'}
									{#if errorId === row.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
									<MailHostDetail account={row} {offline} onSetKey={() => requestAppKey({ name: row.provider_display_name, key_refs: row.mailhost?.key_refs }, true)} onWhere={() => { whereApprove = false; whereRow = row; }} onRemove={() => confirmingRemove = row} onChanged={() => closeRow(row.id)} />
									{:else}
									{#if errorId === row.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
									<p class="capabilities"><span class="capability-label">Reaches</span><span>{capabilityLabels(row)}</span></p>
									{#if microsoftCause(row, 'admin_approval_required')}<p class="impact">Your organization needs to approve CORSIA before it can connect this account. Send this to your IT admin, then press Sign in.</p>{:else if microsoftCause(row, 'consent_cancelled')}<p class="impact">Sign-in was cancelled. Press Sign in to try again.</p>{:else if microsoftCause(row, 'refresh_token_invalid')}<p class="impact">Microsoft signed you out, so agents can’t use this account until you sign in again.</p>{:else if microsoftCause(row, 'provider_unavailable')}<p class="impact">Microsoft had a problem with sign-in. Try again.</p>{:else if microsoftCause(row, 'registration_misconfigured')}<p class="impact">Your Microsoft app for CORSIA isn’t set up correctly. Ask CORSIA in chat to fix it, then press Sign in.</p>{:else if row.state === 'needs_sign_in'}<p class="impact">{row.provider_display_name} signed you out, so agents can’t use this account until you sign in again.</p>{/if}
									{#if manualStep?.id === row.id}<AccountManualNotice account={manualStep} />{/if}
									{#if offline}<p class="offline-action-note" role="status">Actions are unavailable while offline.</p>{/if}
									<div class="actions account-actions">
										{#if microsoftCause(row, 'admin_approval_required')}<span class="row-action"><Button variant="primary" size="sm" disabled={offline || !adminApprovalMessage(row)} onclick={() => void copyAdminMessage(row)}>Copy message for IT</Button></span><span class="row-action" class:deep-link-focus={linkedRowId === row.id && !offline} data-chat-link-focus={linkedRowId === row.id && !offline || undefined} onfocusout={clearLinkedFocus}><Button variant="ghost" size="sm" disabled={offline} loading={actionId === row.id} onclick={() => { clearLinkedFocus(); void startSignIn(row); }}>Sign in</Button></span>{:else if accountNeedsSignIn(row)}<span class="row-action" class:deep-link-focus={linkedRowId === row.id && !offline} data-chat-link-focus={linkedRowId === row.id && !offline || undefined} onfocusout={clearLinkedFocus}><Button variant={microsoftCause(row, 'registration_misconfigured') ? 'ghost' : 'primary'} size="sm" disabled={offline} loading={actionId === row.id} onclick={() => { clearLinkedFocus(); void startSignIn(row); }}>{microsoftCause(row, 'provider_unavailable') ? 'Retry' : 'Sign in'}</Button></span>{/if}
										<span class="row-action" class:deep-link-focus={linkedRowId === row.id && !accountNeedsSignIn(row) && !offline} data-chat-link-focus={linkedRowId === row.id && !accountNeedsSignIn(row) && !offline || undefined} onfocusout={clearLinkedFocus}><Button variant="ghost" size="sm" disabled={offline} onclick={() => { clearLinkedFocus(); whereApprove = false; whereRow = row; }}>Where it works…</Button></span>
										{#if row.state === 'connected'}<span class="row-action"><Button variant="ghost" size="sm" disabled={offline} onclick={() => confirmingOut = row}>Sign out</Button></span>{/if}
										<span class="danger-text row-action"><Button variant="ghost" size="sm" disabled={offline} loading={actionId === row.id} onclick={() => confirmingRemove = row}>Remove</Button></span>
									</div>
									{#if copiedAdminMessageId === row.id}<p class="copied" role="status">Message copied</p>{/if}
									{/if}
								</div>
							{/if}
						</div>
					{:else if row.state === 'awaiting_approval'}
						<div class="item" data-connected-app-id={row.id}>
							<SettingsRow name={row.name} subTone={tone(row)} stackActions>
								{#snippet sub()}{@render appSecondLine(row)}{#if row.notice}<span class="line">{row.notice}</span>{/if}{/snippet}
								{#snippet actions()}<span class="row-action" class:deep-link-focus={linkedRowId === row.id} data-chat-link-focus={linkedRowId === row.id || undefined} onfocusout={clearLinkedFocus}>{#if needsPlace(row)}<Button variant="primary" size="sm" onclick={() => { clearLinkedFocus(); whereApprove = true; whereRow = row; }}>Choose where it works…</Button>{:else}<Button variant="primary" size="sm" loading={isBusy(row.id, 'approve')} disabled={isBusy(row.id, 'decline')} onclick={() => { clearLinkedFocus(); void runApp(row, 'approve'); }}>Approve</Button>{/if}</span><Button variant="ghost" size="sm" loading={isBusy(row.id, 'decline')} disabled={isBusy(row.id, 'approve')} onclick={() => runApp(row, 'decline')}>Decline</Button>{/snippet}
							</SettingsRow>
							{#if errorId === row.id && actionError && !rowBusy(row.id)}<p class="error" role="alert">{actionError}</p>{/if}
							{#if hasSignInToShow(row)}<div class="details"><AppSignInPanel app={row} {signInHere} confirmOut={confirmingAppOut === row.id} onConfirmOut={(on) => confirmingAppOut = on ? row.id : null} onChange={(patch) => replaceApp({ ...row, ...patch })} onRefresh={refresh} onSignedOut={() => signedOut = new Set(signedOut).add(row.id)} /></div>{/if}
						</div>
					{:else}
						<div class="item" data-connected-app-id={row.id}>
							{#snippet appTitle()}{@const account = appAccount(row)}{#if account}<AppAccountTitle name={row.name} {account} />{/if}{/snippet}
							<SettingsRow name={appAccount(row) ? appTitle : row.name} ariaLabel={appAccount(row) ? `${row.name} — ${appAccount(row)}` : undefined} subTone={tone(row)} chevron expanded={isOpenApp(row)} stackActions onOpen={() => openId = openId === row.id ? null : row.id}>
								{#snippet sub()}{@render appSecondLine(row)}{/snippet}
								{#snippet actions()}
									{#if !isOpenApp(row) && row.state === 'needs_token'}<Button variant="ghost" size="sm" onclick={() => requestAppKey(row)}>Set key</Button>
									{:else if !isOpenApp(row) && row.state === 'token_rejected'}<Button variant="ghost" size="sm" onclick={() => requestAppKey(row)}>Replace key</Button>{/if}
								{/snippet}
							</SettingsRow>
							{#if isOpenApp(row)}<div class="details">{#if errorId === row.id && actionError && !rowBusy(row.id)}<p class="error" role="alert">{actionError}</p>{/if}<AppSignInPanel app={row} {signInHere} confirmOut={confirmingAppOut === row.id} onConfirmOut={(on) => confirmingAppOut = on ? row.id : null} onChange={(patch) => replaceApp({ ...row, ...patch })} onRefresh={refresh} onSignedOut={() => signedOut = new Set(signedOut).add(row.id)} autoStart={signInAfterApprove === row.id} onAutoStarted={() => signInAfterApprove = null} />{#if row.state === 'needs_token'}<p class="key-detail">{row.name} needs a key before agents can use it: a code {row.name} gives you so other apps can use your account ({row.name} may call it a token or an API key). Create one in your {row.name} settings, then press Set key.</p>{:else if row.state === 'token_rejected'}<p class="key-detail">{row.name} didn't accept its key. Create a new one in your {row.name} settings, then press Replace key.</p>{:else if row.state === 'failed' && row.failure_class === 'runtime_download_failed'}<p class="key-detail">{runtimeDownloadFailureBody}</p>{/if}<div class="actions">{#if row.state === 'expired'}<Button variant="ghost" size="sm" loading={isBusy(row.id, 'decline')} onclick={() => runApp(row, 'decline')}>Dismiss</Button>{:else}{#if row.state === 'needs_token'}<Button variant="ghost" size="sm" onclick={() => requestAppKey(row)}>Set key</Button>{:else if row.state === 'token_rejected'}<Button variant="ghost" size="sm" onclick={() => requestAppKey(row)}>Replace key</Button>{/if}<Button variant="ghost" size="sm" onclick={() => { whereApprove = false; whereRow = row; }}>Where it works…</Button>{#if row.state !== 'needs_token' && row.state !== 'token_rejected' && row.state !== 'needs_sign_in'}<Button variant="ghost" size="sm" loading={isBusy(row.id, 'reconnect')} disabled={isBusy(row.id, 'remove')} onclick={() => runApp(row, 'reconnect')}>Reconnect</Button>{/if}{#if row.sign_in?.signed_in && confirmingAppOut !== row.id}<Button variant="ghost" size="sm" onclick={() => confirmingAppOut = row.id}>Sign out</Button>{/if}<span class="danger-text"><Button variant="ghost" size="sm" loading={isBusy(row.id, 'remove')} onclick={() => runApp(row, 'remove')}>Remove</Button></span>{/if}</div></div>{/if}
						</div>
					{/if}
				{/each}
			</div>
		{/if}
	{/if}
</section>
{#if removedManualStep}<AccountManualNotice account={removedManualStep} removed onDismiss={() => removedManualStep = null} />{/if}
{#if whereRow}<WhereItWorks app={whereRow} approve={whereApprove} onClose={() => whereRow = null} onSaved={(row) => { if (whereApprove && !isAccount(row)) approved(row); else replace(row); whereRow = null; }} />{/if}
{#if connectAppOpen}<ConnectAppFlow onClose={() => connectAppOpen = false} onConnected={(app) => { rows = [...rows, { ...app, kind: 'hosted_app' }]; }} onSignIn={(app) => { openId = app.id; signInAfterApprove = app.id; }} />{/if}
{#if confirmingOut}<ConfirmDialog open title={`Sign out of ${accountName(confirmingOut)}?`} description="Agents will no longer be able to use this account, including in chats already running. You can sign in again any time." confirmLabel="Sign out" onConfirm={() => signOut(confirmingOut!)} onCancel={() => confirmingOut = null} />{/if}
{#if confirmingRemove}<ConfirmDialog open title={`Remove ${accountName(confirmingRemove)}?`} description={confirmingRemove.provider === 'mailhost' ? `Agents lose access to it. The app password still works until you delete it in your ${confirmingRemove.provider_display_name} account settings.` : confirmingRemove.provider === 'microsoft' ? 'Agents will no longer be able to use this account. CORSIA deletes its sign-in from this computer. Microsoft may still list CORSIA as allowed; remove it on Microsoft’s page.' : confirmingRemove.provider === 'google' ? 'Agents will no longer be able to use this account, including in chats already running. This signs CORSIA out and revokes CORSIA’s access to this Google account.' : ''} confirmLabel="Remove" destructive onConfirm={() => remove(confirmingRemove!)} onCancel={() => confirmingRemove = null} />{/if}

<style>
	.connected-apps { width: 100%; scroll-margin-top: var(--space-5); }
	.heading { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
	@media (min-width: 48.0625rem) { .heading > div { flex: 1; min-width: 0; } }
	.lede, .state, .offline { margin: 0 0 var(--space-2); padding: 0 var(--space-2); }
	.lede { color: var(--text-tertiary); font-size: var(--text-caption); line-height: 1.45; }
	.state, .offline { color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; }
	.state { margin-top: var(--space-5); display: grid; gap: var(--space-3); justify-items: start; }
	.state p { margin: 0; }
	.offline { display: flex; align-items: center; gap: var(--space-1); }
	.offline-action-note { margin: 0 0 var(--space-2); color: var(--text-tertiary); font-size: var(--text-caption); line-height: var(--lh-normal); }
	.rows { display: flex; flex-direction: column; gap: var(--space-1); margin-top: var(--space-4); }
	.place, .line { color: var(--text-tertiary); }
	.line { display: block; }
	.details { padding: var(--space-1) var(--space-2) var(--space-3); }
	.actions { display: flex; flex-wrap: wrap; gap: var(--space-1); margin-left: calc(-1 * var(--button-pad-x-sm)); }
	.row-action :global(.btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	.danger-text :global(.btn.ghost), .danger-text :global(.btn.ghost:hover:not(:disabled)) { color: var(--red-text); }
	.key-detail { color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; margin: 0 0 var(--space-2); }
	.error { color: var(--red-text); margin: 0 0 var(--space-2); font-size: var(--text-footnote); }
	.copied { margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.impact { margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.capabilities { display: flex; gap: var(--space-2); margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-caption); line-height: var(--lh-normal); }
	.capability-label { color: var(--text-tertiary); }
	.account-open-name { min-width: 0; font-size: var(--text-subhead); color: var(--text-primary); overflow-wrap: anywhere; }
	.row-action { position: relative; display: inline-flex; }
	/* A chat link moves focus here after opening the account, so retain a visible arrival mark even without keyboard modality. */
	.row-action.deep-link-focus :global(.btn) { outline: medium solid var(--accent); outline-offset: var(--space-1); }
	.row-action::after { content: ''; position: absolute; inset: calc(-1 * var(--space-2)); }
	.row-action :global(button) { position: relative; z-index: 1; }
</style>
