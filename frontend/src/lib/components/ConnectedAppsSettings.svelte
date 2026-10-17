<script lang="ts">
	import { onMount } from 'svelte';
	import { Button } from '$lib/components/ui';
	import { projects, loadProjects } from '$lib/stores/projects';
	import { settingsAnchor } from '$lib/stores/settingsNavigation';
	import {
		approveConnectedApp, declineConnectedApp, loadConnectedAppsList, reconnectConnectedApp, removeConnectedApp,
		type ConnectedApp,
	} from '$lib/api/connectedApps';
	import { anyFirstSignIn, hasSignInToShow, needsSignInAttention, SIGN_IN_LEDE, signInStateLine } from '$lib/appSignIn';
	import AppSignInPanel from './AppSignInPanel.svelte';
	import WhereItWorks from './WhereItWorks.svelte';

	let apps = $state<ConnectedApp[]>([]);
	let loading = $state(true);
	let error = $state('');
	let actionId = $state<string | null>(null);
	let actionError = $state('');
	let errorId = $state<string | null>(null);
	let whereApp = $state<ConnectedApp | null>(null);
	let whereApprove = $state(false);
	let openId = $state<string | null>(null);
	let signInHere = $state(false);
	let signedOut = $state<Set<string>>(new Set());
	let confirmingOut = $state<string | null>(null);
	/** The app whose approval just came back asking for a sign-in; its panel starts the sign-in once. */
	let signInAfterApprove = $state<string | null>(null);
	const isOpen = (app: ConnectedApp): boolean => openId === app.id || needsSignInAttention(app);

	function stateLine(app: ConnectedApp): string {
		const signIn = signInStateLine(app, signedOut.has(app.id));
		if (signIn) return signIn;
		switch (app.state) {
			case 'awaiting_approval': return 'Waiting for your approval';
			case 'connecting': return 'Connecting…';
			case 'working': return `Connected${app.tool_count ? ` · ${app.tool_count} actions` : ''}`;
			case 'needs_token': return 'Needs a key';
			case 'token_rejected': return 'Key not accepted';
			case 'needs_sign_in': return 'Needs you to sign in';
			case 'disconnected': return 'Disconnected';
			case 'expired': return 'Request expired';
			case 'failed': return `Couldn't connect: ${failureLine(app.failure_class)}`;
			default: return app.state;
		}
	}

	function failureLine(failure: string | null | undefined): string {
		return ({ cannot_start: "app didn't start", timed_out: 'no response', protocol_error: 'not compatible', name_collision: 'clashes with another app' }[failure ?? ''] ?? 'unknown reason');
	}

	function needsPlace(app: ConnectedApp): boolean {
		return app.state === 'awaiting_approval' && app.reach.mode === 'selected' && app.reach.project_ids.length === 0 && !app.reach.outside_projects;
	}

	function placeLine(app: ConnectedApp): string {
		if (app.reach.mode === 'all') return 'All projects';
		const names = [
			...$projects.filter((project) => app.reach.project_ids.includes(project.id)).map((project) => project.name),
			...(app.reach.outside_projects ? ['Outside projects'] : []),
		];
		if (names.length === 0) return 'Choose where it works';
		return names.length > 2 ? `${names.slice(0, 2).join(', ')}, ${names.length - 2} more` : names.join(', ');
	}

	async function load(): Promise<void> {
		loading = true;
		error = '';
		try { ({ servers: apps, signInHere } = await loadConnectedAppsList()); }
		catch (cause) { error = cause instanceof Error ? cause.message : 'Could not load connected apps.'; }
		finally { loading = false; }
	}

	/** Re-read the rows without the loading state, so an open row keeps its place while a sign-in finishes. */
	async function refresh(): Promise<void> {
		try { ({ servers: apps, signInHere } = await loadConnectedAppsList()); } catch { /* the next refresh tries again */ }
	}

	async function run(app: ConnectedApp, action: 'approve' | 'decline' | 'reconnect' | 'remove'): Promise<void> {
		actionId = app.id;
		errorId = app.id;
		actionError = '';
		try {
			if (action === 'approve') approved(await approveConnectedApp(app));
			else if (action === 'decline') {
				// Declining a new request deletes it, so the answer is `removed` and the row goes; a pending change on a working app comes back as that app.
				const next = await declineConnectedApp(app.id);
				if (next.removed) apps = apps.filter((row) => row.id !== app.id); else replace(next);
			}
			else if (action === 'reconnect') replace(await reconnectConnectedApp(app.id));
			else { await removeConnectedApp(app.id); apps = apps.filter((row) => row.id !== app.id); }
		} catch (cause) {
			actionError = cause instanceof Error ? cause.message : 'Could not update this app. Try again.';
		} finally { actionId = null; }
	}

	function replace(next: ConnectedApp): void { apps = apps.map((app) => app.id === next.id ? { ...app, ...next } : app); }
	function approved(next: ConnectedApp): void {
		replace(next);
		if (next.state === 'needs_sign_in') signInAfterApprove = next.id;
	}
	onMount(() => { void loadProjects(); void load(); });
</script>

<section class="connected-apps" data-settings-anchor="connected-apps" use:settingsAnchor={'connected-apps'} id="connected-apps">
	<div class="heading"><div><h2>Connected apps</h2><p>Connect the apps you already use, like GitHub or Notion, so your agents can work in them. Each app works in the projects you choose for it.</p>{#if anyFirstSignIn(apps)}<p>{SIGN_IN_LEDE}</p>{/if}</div></div>
	{#if loading}
		<p class="state" role="status">Loading connected apps…</p>
	{:else if error}
		<div class="state" role="alert"><p>{error}</p><Button onclick={load}>Try again</Button></div>
	{:else if apps.length === 0}
		<p class="state">No connected apps yet. Ask CORSIA in chat to bring over the apps you use in Claude Code, Claude Desktop, Cursor, Codex, OpenClaw or Hermes Agent, for example: “Bring over my apps from Claude.”</p>
	{:else}
		<div class="rows">
			{#each apps as app (app.id)}
				{#if app.state === 'awaiting_approval'}
				<article class="waiting-row">
					<div class="waiting-copy">
						<strong title={app.name}>{app.name}</strong>
						<span class="second status">{stateLine(app)}{#if !needsPlace(app)}<span class="place">{` · ${placeLine(app)}`}</span>{/if}</span>
						{#if app.folder}<span class="second">From Claude Code · {app.folder} folder</span>{/if}
						{#if app.notice}<span class="second">{app.notice}</span>{/if}
					</div>
					<div class="waiting-actions">
						{#if needsPlace(app)}
							<Button variant="primary" size="sm" onclick={() => { whereApprove = true; whereApp = app; }}>Choose where it works…</Button>
						{:else}
							<Button variant="primary" size="sm" loading={actionId === app.id} onclick={() => run(app, 'approve')}>Approve</Button>
						{/if}
						<Button variant="ghost" size="sm" loading={actionId === app.id} onclick={() => run(app, 'decline')}>Decline</Button>
					</div>
					{#if errorId === app.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
					{#if hasSignInToShow(app)}
						<div class="waiting-panel"><AppSignInPanel {app} {signInHere} confirmOut={confirmingOut === app.id} onConfirmOut={(on) => confirmingOut = on ? app.id : null} onChange={(patch) => replace({ ...app, ...patch })} onRefresh={refresh}
							onSignedOut={() => signedOut = new Set(signedOut).add(app.id)} /></div>
					{/if}
				</article>
				{:else}
				<article class:open={isOpen(app)} class="app-row">
					<button class="summary press-tint" aria-expanded={isOpen(app)} onclick={() => openId = openId === app.id ? null : app.id}>
						<span class="app-copy waiting-copy"><strong title={app.name}>{app.name}</strong><span class="second" class:status={app.state !== 'working'}>{stateLine(app)}{#if !needsPlace(app)}<span class="place">{` · ${placeLine(app)}`}</span>{/if}</span></span><span aria-hidden="true">›</span>
					</button>
					{#if isOpen(app)}
						<div class="details">
							{#if errorId === app.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
							<AppSignInPanel {app} {signInHere} confirmOut={confirmingOut === app.id} onConfirmOut={(on) => confirmingOut = on ? app.id : null} onChange={(patch) => replace({ ...app, ...patch })} onRefresh={refresh}
								onSignedOut={() => signedOut = new Set(signedOut).add(app.id)} autoStart={signInAfterApprove === app.id} onAutoStarted={() => signInAfterApprove = null} />
							<div class="actions">
								{#if app.state === 'expired'}
									<!-- A request that ran out can't be placed, reconnected or approved; Dismiss runs Decline's cleanup (copy sheet). -->
									<Button loading={actionId === app.id} onclick={() => run(app, 'decline')}>Dismiss</Button>
								{:else}
									<Button onclick={() => { whereApprove = false; whereApp = app; }}>Where it works…</Button>
									<Button loading={actionId === app.id} onclick={() => run(app, 'reconnect')}>Reconnect</Button>
									{#if app.sign_in?.signed_in && confirmingOut !== app.id}<Button onclick={() => confirmingOut = app.id}>Sign out</Button>{/if}
									<Button variant="danger" loading={actionId === app.id} onclick={() => run(app, 'remove')}>Remove</Button>
								{/if}
							</div>
						</div>
					{/if}
				</article>
				{/if}
			{/each}
		</div>
	{/if}
</section>

{#if whereApp}<WhereItWorks app={whereApp} approve={whereApprove} onClose={() => whereApp = null} onSaved={(app) => { if (whereApprove) approved(app); else replace(app); whereApp = null; }} />{/if}

<style>
	.connected-apps { width: 100%; margin-top: var(--space-7); scroll-margin-top: var(--space-5); }
	.heading h2 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); }
	.heading p { margin: var(--space-2) 0 0; color: var(--text-secondary); line-height: 1.5; }
	.rows { display: grid; gap: var(--space-2); margin-top: var(--space-5); }
	/* A request waiting on the person is a flat list row: status on the second line, one solid action, a text action. */
	.waiting-row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-2) var(--space-4); min-height: var(--list-row-min-height); padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); }
	.waiting-copy { min-width: 0; flex: 1 1 14rem; display: grid; gap: 2px; }
	.waiting-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); font-size: var(--text-body); font-weight: var(--fw-regular); }
	.second { color: var(--text-tertiary); font-size: var(--text-caption); line-height: var(--lh-normal); overflow-wrap: anywhere; }
	.second.status { color: var(--amber-text); }
	.second.status .place { color: var(--text-tertiary); }
	.waiting-actions { display: flex; align-items: center; gap: var(--space-2); }
	.waiting-row .error, .waiting-panel { flex-basis: 100%; margin: 0; }
	/* Connected and waiting apps are one flat list; the chevron still discloses each connected app's details. */
	.app-row { min-height: var(--list-row-min-height); }
	.summary { appearance: none; border: 0; background: none; color: inherit; width: 100%; padding: var(--space-2) var(--space-3); display: flex; justify-content: space-between; align-items: center; cursor: pointer; text-align: left; }
	.app-copy { min-width: 0; display: grid; gap: var(--space-1); }
	.app-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
	.state { color: var(--text-secondary); font-size: var(--text-footnote); }
	.details { padding: var(--space-2) 0 var(--space-2) var(--space-3); }
	.actions { display: flex; justify-content: flex-end; gap: var(--space-2); flex-wrap: wrap; }
	.error { color: var(--red); margin: 0 0 var(--space-2); font-size: var(--text-footnote); }
	.state { margin: var(--space-5) 0; display: grid; gap: var(--space-3); }
	@media (max-width: 768px) { .connected-apps { margin-top: var(--space-5); } }
</style>
