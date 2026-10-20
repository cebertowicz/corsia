<script lang="ts">
	import { onMount } from 'svelte';
	import { Button } from '$lib/components/ui';
	import { projects, loadProjects } from '$lib/stores/projects';
	import { requestAppKey, settingsAnchor, settingsNavigation } from '$lib/stores/settingsNavigation';
	import {
		approveConnectedApp, connectedAppStateLine, declineConnectedApp, loadConnectedAppsRows, reconnectConnectedApp, removeAccount, removeConnectedApp,
		signOutOfAccount, startAccountSignIn, type AccountSignOutResult, type ConnectedAccount, type ConnectedApp,
		type ConnectedAppsRow, isOnlyCorsia, needsManualGoogleStep,
	} from '$lib/api/connectedApps';
	import { anyFirstSignIn, hasSignInToShow, needsSignInAttention, SIGN_IN_LEDE } from '$lib/appSignIn';
	import AppSignInPanel from './AppSignInPanel.svelte';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import SettingsRow from './SettingsRow.svelte';
	import WhereItWorks from './WhereItWorks.svelte';
	import ConnectAppFlow from './ConnectAppFlow.svelte';
	type AccountAddress = { localPrefix: string; localSuffix: string; domain: string };
	const providerName = (account: ConnectedAccount) => account.provider === 'microsoft' ? 'Microsoft' : 'Google';
	const accountName = (account: ConnectedAccount) => `${providerName(account)} — ${account.address}`;
	const accountAnchor = (id: string) => `connected-account-${id}`;
	const isAccount = (row: ConnectedAppsRow): row is ConnectedAccount => row.kind === 'account';
	const accountNeedsSignIn = (account: ConnectedAccount) => account.state === 'needs_sign_in' || account.state === 'signed_out';
	const isOpenApp = (app: ConnectedApp): boolean => openId === app.id || needsSignInAttention(app);
	let rows = $state<ConnectedAppsRow[]>([]);
	let loading = $state(true);
	let offline = $state(false);
	let error = $state('');
	let actionId = $state<string | null>(null);
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
	let closedLocalPrefixes = $state(new Map<string, string>());
	let linkedAccountId = $state<string | null>(null);
	let copiedAdminMessageId = $state<string | null>(null);
	let textContext: CanvasRenderingContext2D | null = null;
	const stateLine = (app: ConnectedApp) => connectedAppStateLine(app, signedOut.has(app.id));
	const isMicrosoft = (account: ConnectedAccount) => account.provider === 'microsoft';
	const microsoftCause = (account: ConnectedAccount, cause: string) => isMicrosoft(account) && account.cause === cause;
	function accountStateLine(account: ConnectedAccount): string {
		if (microsoftCause(account, 'admin_approval_required')) return 'Needs admin approval';
		if (microsoftCause(account, 'provider_unavailable')) return 'Sign-in didn’t finish';
		switch (account.state) {
			case 'connected': return 'Connected';
			case 'needs_sign_in': return 'Needs you to sign in';
			case 'signed_out': return 'Signed out';
		}
	}
	function tone(row: ConnectedAppsRow): 'quiet' | 'failed' | 'attention' {
		if (isAccount(row)) return microsoftCause(row, 'provider_unavailable') ? 'failed' : row.state === 'connected' || row.state === 'signed_out' ? 'quiet' : 'attention';
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
	function replaceApp(next: ConnectedApp): void { replace({ ...next, kind: 'hosted_app' }); }
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
		actionId = app.id; errorId = app.id; actionError = '';
		try {
			if (action === 'approve') approved(await approveConnectedApp(app));
			else if (action === 'decline') {
				const next = await declineConnectedApp(app.id);
				if (next.removed) rows = rows.filter((row) => row.id !== app.id); else replaceApp(next);
			} else if (action === 'reconnect') replaceApp(await reconnectConnectedApp(app.id));
			else { await removeConnectedApp(app.id); rows = rows.filter((row) => row.id !== app.id); }
		} catch (cause) { actionError = cause instanceof Error ? cause.message : "Couldn't update this app. Try again."; }
		finally { actionId = null; }
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
		if (!account.client_id) return '';
		return `Please approve my CORSIA app for my account, ${account.address}. It is my own Microsoft app registration, Application (client) ID ${account.client_id}, and requests delegated Microsoft Graph access to Mail.ReadWrite, Mail.Send, Calendars.ReadWrite, Files.ReadWrite, offline_access and User.Read.`;
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
			manualStep = row.provider === 'google' && needsManualGoogleStep(result.revocation) ? row : null;
		} catch (cause) { actionError = cause instanceof Error ? cause.message : `Couldn't change this ${providerName(account)} account. Try again.`; }
		finally { confirmingOut = null; actionId = null; }
	}
	async function remove(account: ConnectedAccount): Promise<void> {
		actionId = account.id; errorId = account.id; actionError = '';
		try {
			const result = await removeAccount(account);
			rows = rows.filter((row) => row.id !== account.id);
			removedManualStep = account.provider === 'microsoft' || needsManualGoogleStep(result.revocation) ? account : null;
		}
		catch (cause) { actionError = cause instanceof Error ? cause.message : `Couldn't change this ${providerName(account)} account. Try again.`; }
		finally { confirmingRemove = null; actionId = null; }
	}
	function accountAddress(address: string): AccountAddress | null {
		const at = address.lastIndexOf('@');
		if (at <= 0 || at === address.length - 1) return null;
		const local = address.slice(0, at);
		const suffixStart = Math.max(local.lastIndexOf('.'), local.lastIndexOf('_'), local.lastIndexOf('-'), local.lastIndexOf('+'));
		return { localPrefix: suffixStart > 0 ? local.slice(0, suffixStart) : local, localSuffix: suffixStart > 0 ? local.slice(suffixStart) : '', domain: address.slice(at + 1) };
	}
	function localSegments(prefix: string): string[] { return prefix.split(/[._+\-]+/).filter(Boolean); }
	function width(text: string, style: CSSStyleDeclaration): number {
		if (!textContext && typeof document !== 'undefined') textContext = document.createElement('canvas').getContext('2d');
		if (!textContext) return text.length * 8;
		textContext.font = style.font || `${style.fontSize} ${style.fontFamily}`;
		const letterSpacing = Number.parseFloat(style.letterSpacing) || 0;
		return textContext.measureText(text).width + Math.max(0, text.length - 1) * letterSpacing;
	}
	function clippedPrefix(prefix: string, available: number, style: CSSStyleDeclaration): string {
		if (width(prefix, style) <= available) return prefix;
		const chars = [...prefix];
		let low = 0; let high = chars.length;
		while (low < high) {
			const middle = Math.ceil((low + high) / 2);
			if (width(`${chars.slice(0, middle).join('')}…`, style) <= available) low = middle;
			else high = middle - 1;
		}
		return `${chars.slice(0, low).join('').replace(/[._+\-]+$/, '')}…`;
	}
	function displayKey(prefix: string, suffix: string, domain: string): string {
		return `${prefix}${prefix.includes('…') && /^[._+\-]/.test(suffix) ? suffix.slice(1) : suffix}@${domain}`;
	}
	function distinctPrefix(account: ConnectedAccount, available: number, style: CSSStyleDeclaration): string {
		const address = accountAddress(account.address);
		if (!address || width(address.localPrefix, style) <= available) return address?.localPrefix ?? account.address;
		const ownSegments = localSegments(address.localPrefix);
		const siblings = rows.filter(isAccount).filter((candidate) => {
			const other = accountAddress(candidate.address);
			return candidate.id !== account.id && candidate.id !== openId && other?.domain === address.domain && localSegments(other.localPrefix)[0] === ownSegments[0];
		});
		const distinctIndex = ownSegments.findIndex((segment, index) => siblings.every((candidate) => localSegments(accountAddress(candidate.address)!.localPrefix)[index] !== segment));
		if (distinctIndex < 0) return clippedPrefix(address.localPrefix, available, style);
		if (distinctIndex === 0) return clippedPrefix(address.localPrefix, available, style);
		const distinctive = ownSegments[distinctIndex];
		const lead = ownSegments[0];
		const candidate = `${lead}…${distinctive}`;
		if (width(candidate, style) <= available) return candidate;
		for (let length = [...lead].length - 1; length > 0; length--) {
			const shortened = `${[...lead].slice(0, length).join('')}…${distinctive}`;
			if (width(shortened, style) <= available) return shortened;
		}
		return clippedPrefix(address.localPrefix, available, style);
	}
	function observeClosedTitle(node: HTMLElement, account: ConnectedAccount) {
		let current = account;
		let frame = 0;
		const update = () => {
			cancelAnimationFrame(frame);
			frame = requestAnimationFrame(() => {
				if (openId === current.id) return;
				const parts = accountAddress(current.address);
				const prefix = node.querySelector<HTMLElement>('.account-local-prefix');
				const provider = node.querySelector<HTMLElement>('.account-provider');
				const suffix = node.querySelector<HTMLElement>('.account-local-suffix');
				const domain = node.querySelector<HTMLElement>('.account-domain');
				if (!parts || !prefix || !provider || !suffix || !domain) return;
				const providerStyle = getComputedStyle(provider);
				const providerGap = Number.parseFloat(providerStyle.marginInlineEnd) || 0;
				const available = Math.max(0, node.clientWidth - provider.getBoundingClientRect().width - providerGap - suffix.getBoundingClientRect().width - domain.getBoundingClientRect().width);
				const next = distinctPrefix(current, available, getComputedStyle(prefix));
				if (closedLocalPrefixes.get(current.id) === next) return;
				closedLocalPrefixes = new Map(closedLocalPrefixes).set(current.id, next);
				requestAnimationFrame(() => {
					const domainRange = document.createRange();
					if (typeof domainRange.getBoundingClientRect !== 'function') return;
					domainRange.selectNodeContents(domain);
					const overflow = domainRange.getBoundingClientRect().right - node.getBoundingClientRect().right;
					if (overflow <= 0.5) return;
					const fallback = distinctPrefix(current, Math.max(0, available - overflow - 1), getComputedStyle(prefix));
					if (closedLocalPrefixes.get(current.id) !== fallback) closedLocalPrefixes = new Map(closedLocalPrefixes).set(current.id, fallback);
				});
			});
		};
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
		observer?.observe(node); update();
		return { update(next: ConnectedAccount) { current = next; update(); }, destroy() { cancelAnimationFrame(frame); observer?.disconnect(); } };
	}
	function shownPrefix(account: ConnectedAccount): string {
		const parts = accountAddress(account.address);
		return parts ? closedLocalPrefixes.get(account.id) ?? parts.localPrefix : account.address;
	}
	function shownSuffix(account: ConnectedAccount): string {
		const parts = accountAddress(account.address);
		if (!parts) return '';
		const prefix = shownPrefix(account);
		return prefix.endsWith('…') && /^[._+\-]/.test(parts.localSuffix) ? parts.localSuffix.slice(1) : parts.localSuffix;
	}
	function capabilityLabels(account: ConnectedAccount): string { return account.capabilities.map((capability) => ({ mail: 'Email', calendar: 'Calendar', files: 'Files' }[capability])).join(', '); }

	function clearLinkedFocus(): void { linkedAccountId = null; }

	$effect(() => {
		const accountId = $settingsNavigation.connectedAppId;
		if (loading || !accountId || !rows.some((row) => row.id === accountId)) return;
		openId = accountId;
		clearLinkedFocus();
		requestAnimationFrame(() => {
			const row = document.querySelector<HTMLElement>(`[data-connected-account-id="${CSS.escape(accountId)}"]`);
			const account = rows.find((candidate): candidate is ConnectedAccount => candidate.id === accountId && isAccount(candidate));
			const label = account && accountNeedsSignIn(account) ? 'Sign in' : 'Where it works…';
			const target = [...(row?.querySelectorAll<HTMLButtonElement>('button') ?? [])].find((button) => button.textContent?.trim() === label);
			if (!target || target.disabled) return;
			target.focus();
			if (document.activeElement === target) linkedAccountId = accountId;
		});
	});
	$effect(() => { if (offline) clearLinkedFocus(); });
	onMount(() => {
		void loadProjects(); void load();
		const afterSignIn = () => { void refresh(); };
		window.addEventListener('focus', afterSignIn);
		return () => window.removeEventListener('focus', afterSignIn);
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
		<div class="state" role="alert"><p>{error}</p><Button onclick={load}>Try again</Button></div>
	{:else}
		{#if offline}<div class="offline" role="status">Showing last known items. <Button variant="ghost" size="sm" onclick={load}>Try again</Button></div>{/if}
		{#if rows.length === 0}
			<p class="state">No connected apps yet. Connect one here, or ask CORSIA in chat to bring over the apps you use in Claude Code, Claude Desktop, Cursor, Codex, OpenClaw or Hermes Agent, for example: “Bring over my apps from Claude.”</p>
		{:else}
			<div class="rows">
				{#each rows as row (row.id)}
					{#if isAccount(row)}
						<div class="item account-item" data-connected-account-id={row.id} data-settings-anchor={accountAnchor(row.id)} use:settingsAnchor={accountAnchor(row.id)}>
							{#snippet accountTitle()}{#if openId === row.id}<span class="account-open-name">{accountName(row)}</span>{:else}<span class="account-closed-name" use:observeClosedTitle={row}><span class="account-provider">{providerName(row)} —</span><span class="account-local-prefix">{shownPrefix(row)}</span><span class="account-local-suffix">{shownSuffix(row)}</span><span class="account-domain">@{accountAddress(row.address)?.domain}</span></span>{/if}{/snippet}
							<SettingsRow name={accountTitle} ariaLabel={accountName(row)} subTone={tone(row)} wrapName={openId === row.id} chevron expanded={openId === row.id} onOpen={() => openId = openId === row.id ? null : row.id}>
								{#snippet sub()}{accountStateLine(row)}<span class="place">{` · ${placeLine(row)}`}</span>{/snippet}
							</SettingsRow>
							{#if openId === row.id}
								<div class="details">
									{#if errorId === row.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
									<p class="capabilities"><span class="capability-label">Reaches</span><span>{capabilityLabels(row)}</span></p>
									{#if microsoftCause(row, 'admin_approval_required')}<p class="impact">Your organization needs to approve CORSIA before it can connect this account. Send this to your IT admin, then press Sign in.</p>{:else if microsoftCause(row, 'consent_cancelled')}<p class="impact">Sign-in was cancelled. Press Sign in to try again.</p>{:else if microsoftCause(row, 'refresh_token_invalid')}<p class="impact">Microsoft signed you out, so agents can’t use this account until you sign in again.</p>{:else if microsoftCause(row, 'provider_unavailable')}<p class="impact">Microsoft had a problem with sign-in. Try again.</p>{:else if row.state === 'needs_sign_in'}<p class="impact">Google signed you out, so agents can’t use this account until you sign in again.</p>{/if}
									{#if manualStep?.id === row.id}<p class="impact">You’re signed out here, but CORSIA couldn’t reach Google, so Google may still list CORSIA as allowed. To remove it, open your <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener">Google Account</a> and remove CORSIA.</p>{/if}
									{#if copiedAdminMessageId === row.id}<p class="copied" role="status">Message copied</p>{/if}
									{#if offline}<p class="offline-action-note" role="status">Actions are unavailable while offline.</p>{/if}
									<div class="actions account-actions">
										{#if microsoftCause(row, 'admin_approval_required')}<span class="account-action"><Button variant="primary" size="sm" disabled={offline || !adminApprovalMessage(row)} onclick={() => void copyAdminMessage(row)}>Copy message for IT</Button></span><span class="account-action" class:deep-link-focus={linkedAccountId === row.id && !offline} data-chat-link-focus={linkedAccountId === row.id && !offline || undefined} onfocusout={clearLinkedFocus}><Button variant="ghost" size="sm" disabled={offline} loading={actionId === row.id} onclick={() => { clearLinkedFocus(); void startSignIn(row); }}>Sign in</Button></span>{:else if accountNeedsSignIn(row)}<span class="account-action" class:deep-link-focus={linkedAccountId === row.id && !offline} data-chat-link-focus={linkedAccountId === row.id && !offline || undefined} onfocusout={clearLinkedFocus}><Button variant={microsoftCause(row, 'provider_unavailable') ? 'primary' : 'ghost'} size="sm" disabled={offline} loading={actionId === row.id} onclick={() => { clearLinkedFocus(); void startSignIn(row); }}>{microsoftCause(row, 'provider_unavailable') ? 'Try again' : 'Sign in'}</Button></span>{/if}
										<span class="account-action" class:deep-link-focus={linkedAccountId === row.id && !accountNeedsSignIn(row) && !offline} data-chat-link-focus={linkedAccountId === row.id && !accountNeedsSignIn(row) && !offline || undefined} onfocusout={clearLinkedFocus}><Button variant="ghost" size="sm" disabled={offline} onclick={() => { clearLinkedFocus(); whereApprove = false; whereRow = row; }}>Where it works…</Button></span>
										{#if row.state === 'connected'}<span class="account-action"><Button variant="ghost" size="sm" disabled={offline} onclick={() => confirmingOut = row}>Sign out</Button></span>{/if}
										<span class="danger-text account-action"><Button variant="ghost" size="sm" disabled={offline} loading={actionId === row.id} onclick={() => confirmingRemove = row}>Remove</Button></span>
									</div>
								</div>
							{/if}
						</div>
					{:else if row.state === 'awaiting_approval'}
						<div class="item">
							<SettingsRow name={row.name} subTone={tone(row)} stackActions>
								{#snippet sub()}{@render appSecondLine(row)}{#if row.notice}<span class="line">{row.notice}</span>{/if}{/snippet}
								{#snippet actions()}{#if needsPlace(row)}<Button variant="primary" size="sm" onclick={() => { whereApprove = true; whereRow = row; }}>Choose where it works…</Button>{:else}<Button variant="primary" size="sm" loading={actionId === row.id} onclick={() => runApp(row, 'approve')}>Approve</Button>{/if}<Button variant="ghost" size="sm" loading={actionId === row.id} onclick={() => runApp(row, 'decline')}>Decline</Button>{/snippet}
							</SettingsRow>
							{#if errorId === row.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
							{#if hasSignInToShow(row)}<div class="details"><AppSignInPanel app={row} {signInHere} confirmOut={confirmingAppOut === row.id} onConfirmOut={(on) => confirmingAppOut = on ? row.id : null} onChange={(patch) => replaceApp({ ...row, ...patch })} onRefresh={refresh} onSignedOut={() => signedOut = new Set(signedOut).add(row.id)} /></div>{/if}
						</div>
					{:else}
						<div class="item">
							<SettingsRow name={row.name} subTone={tone(row)} chevron expanded={isOpenApp(row)} stackActions onOpen={() => openId = openId === row.id ? null : row.id}>
								{#snippet sub()}{@render appSecondLine(row)}{/snippet}
								{#snippet actions()}
									{#if !isOpenApp(row) && row.state === 'needs_token'}<Button variant="ghost" size="sm" onclick={() => requestAppKey(row)}>Set key</Button>
									{:else if !isOpenApp(row) && row.state === 'token_rejected'}<Button variant="ghost" size="sm" onclick={() => requestAppKey(row)}>Replace key</Button>{/if}
								{/snippet}
							</SettingsRow>
							{#if isOpenApp(row)}<div class="details">{#if errorId === row.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}<AppSignInPanel app={row} {signInHere} confirmOut={confirmingAppOut === row.id} onConfirmOut={(on) => confirmingAppOut = on ? row.id : null} onChange={(patch) => replaceApp({ ...row, ...patch })} onRefresh={refresh} onSignedOut={() => signedOut = new Set(signedOut).add(row.id)} autoStart={signInAfterApprove === row.id} onAutoStarted={() => signInAfterApprove = null} />{#if row.state === 'needs_token'}<p class="key-detail">{row.name} needs a key before agents can use it: a code {row.name} gives you so other apps can use your account ({row.name} may call it a token or an API key). Create one in your {row.name} settings. Then press Set key next to {row.name}. That opens the Keys page: paste the key there and press Set.</p>{:else if row.state === 'token_rejected'}<p class="key-detail">{row.name} didn't accept its key. Create a new one in your {row.name} settings, then press Replace key.</p>{/if}<div class="actions">{#if row.state === 'expired'}<Button variant="ghost" size="sm" loading={actionId === row.id} onclick={() => runApp(row, 'decline')}>Dismiss</Button>{:else}{#if row.state === 'needs_token'}<Button variant="ghost" size="sm" onclick={() => requestAppKey(row)}>Set key</Button>{:else if row.state === 'token_rejected'}<Button variant="ghost" size="sm" onclick={() => requestAppKey(row)}>Replace key</Button>{/if}<Button variant="ghost" size="sm" onclick={() => { whereApprove = false; whereRow = row; }}>Where it works…</Button>{#if row.state !== 'needs_token' && row.state !== 'token_rejected'}<Button variant="ghost" size="sm" loading={actionId === row.id} onclick={() => runApp(row, 'reconnect')}>Reconnect</Button>{/if}{#if row.sign_in?.signed_in && confirmingAppOut !== row.id}<Button variant="ghost" size="sm" onclick={() => confirmingAppOut = row.id}>Sign out</Button>{/if}<span class="danger-text"><Button variant="ghost" size="sm" loading={actionId === row.id} onclick={() => runApp(row, 'remove')}>Remove</Button></span>{/if}</div></div>{/if}
						</div>
					{/if}
				{/each}
			</div>
		{/if}
	{/if}
</section>
{#if removedManualStep}{#if removedManualStep.provider === 'microsoft'}<p class="manual-removal" role="status">Microsoft may still list CORSIA as allowed for {removedManualStep.address}. <a href={removedManualStep.account_kind === 'personal' ? 'https://account.live.com/consent/Manage' : 'https://myapps.microsoft.com'} target="_blank" rel="noopener" onclick={() => removedManualStep = null}>Manage Microsoft permissions</a></p>{:else}<p class="manual-removal" role="status">CORSIA removed {accountName(removedManualStep)} here, but couldn’t reach Google. To remove it there, open your <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener">Google Account</a> and remove CORSIA.</p>{/if}{/if}

{#if whereRow}<WhereItWorks app={whereRow} approve={whereApprove} onClose={() => whereRow = null} onSaved={(row) => { if (whereApprove && !isAccount(row)) approved(row); else replace(row); whereRow = null; }} />{/if}
{#if connectAppOpen}<ConnectAppFlow onClose={() => connectAppOpen = false} onConnected={(app) => { rows = [...rows, { ...app, kind: 'hosted_app' }]; }} onSignIn={(app) => { openId = app.id; signInAfterApprove = app.id; }} />{/if}
{#if confirmingOut}<ConfirmDialog open title={`Sign out of ${accountName(confirmingOut)}?`} description="Agents will no longer be able to use this account, including in conversations already running. You can sign in again any time." confirmLabel="Sign out" onConfirm={() => signOut(confirmingOut!)} onCancel={() => confirmingOut = null} />{/if}
{#if confirmingRemove}<ConfirmDialog open title={`Remove ${accountName(confirmingRemove)}?`} description={confirmingRemove.provider === 'microsoft' ? 'Agents will no longer be able to use this account. CORSIA deletes its sign-in from this computer. Microsoft may still list CORSIA as allowed; remove it on Microsoft’s page.' : 'Agents will no longer be able to use this account, including in conversations already running. This signs CORSIA out and revokes CORSIA’s access to this Google account.'} confirmLabel="Remove" destructive onConfirm={() => remove(confirmingRemove!)} onCancel={() => confirmingRemove = null} />{/if}

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
	.account-action :global(.btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	.danger-text :global(.btn.ghost), .danger-text :global(.btn.ghost:hover:not(:disabled)) { color: var(--red-text); }
	.key-detail { color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; margin: 0 0 var(--space-2); }
	.error { color: var(--red); margin: 0 0 var(--space-2); font-size: var(--text-footnote); }
	.copied { margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.manual-removal { margin: var(--space-3) var(--space-2) 0; color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.impact { margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.capabilities { display: flex; gap: var(--space-2); margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-caption); line-height: var(--lh-normal); }
	.capability-label { color: var(--text-tertiary); }
	.account-closed-name, .account-open-name { min-width: 0; font-size: var(--text-subhead); color: var(--text-primary); }
	.account-closed-name { display: flex; overflow: hidden; white-space: nowrap; }
	.account-provider, .account-local-prefix, .account-local-suffix, .account-domain { flex: 0 0 auto; }
	.account-provider { margin-inline-end: var(--space-1); }
	.account-local-prefix { overflow: hidden; text-overflow: clip; }
	.account-open-name { overflow-wrap: anywhere; }
	.account-action { position: relative; display: inline-flex; }
	/* A chat link moves focus here after opening the account, so retain a visible arrival mark even without keyboard modality. */
	.account-action.deep-link-focus :global(.btn) { outline: medium solid var(--accent); outline-offset: var(--space-1); }
	.account-action::after { content: ''; position: absolute; inset: calc(-1 * var(--space-2)); }
	.account-action :global(button) { position: relative; z-index: 1; }
</style>
