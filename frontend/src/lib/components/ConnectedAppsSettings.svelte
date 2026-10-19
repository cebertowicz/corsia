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
	import SettingsRow from './SettingsRow.svelte';
	import WhereItWorks from './WhereItWorks.svelte';
	import ConnectAppFlow from './ConnectAppFlow.svelte';

	let apps = $state<ConnectedApp[]>([]);
	let loading = $state(true);
	let error = $state('');
	let actionId = $state<string | null>(null);
	let actionError = $state('');
	let errorId = $state<string | null>(null);
	let whereApp = $state<ConnectedApp | null>(null);
	let whereApprove = $state(false);
	let connectAppOpen = $state(false);
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

	/** A failure takes the failed colour, a state waiting on the person the attention colour; a working app stays quiet. */
	function tone(app: ConnectedApp): 'quiet' | 'failed' | 'attention' {
		if (app.state === 'failed' || (app.sign_in?.status === 'failed' && !app.sign_in.signed_in)) return 'failed';
		return app.state === 'working' ? 'quiet' : 'attention';
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

{#snippet secondLine(app: ConnectedApp)}{stateLine(app)}{#if !needsPlace(app)}<span class="place">{` · ${placeLine(app)}`}</span>{/if}{#if app.folder}<span class="line">From Claude Code · {app.folder} folder</span>{/if}{/snippet}

<section class="connected-apps" data-settings-anchor="connected-apps" use:settingsAnchor={'connected-apps'} id="connected-apps">
	<div class="heading">
		<div>
			<p class="lede">Connect the apps you already use, like GitHub or Notion, so your agents can work in them. Each app works in the projects you choose for it.</p>
			{#if anyFirstSignIn(apps)}<p class="lede">{SIGN_IN_LEDE}</p>{/if}
		</div>
		<Button variant="primary" onclick={() => connectAppOpen = true}>Connect an app</Button>
	</div>
	{#if loading}
		<p class="state" role="status">Loading connected apps…</p>
	{:else if error}
		<div class="state" role="alert"><p>{error}</p><Button onclick={load}>Try again</Button></div>
	{:else if apps.length === 0}
		<p class="state">No connected apps yet. Connect one here, or ask CORSIA in chat to bring over the apps you use in Claude Code, Claude Desktop, Cursor or Codex, for example: “Bring over my apps from Claude.”</p>
	{:else}
		<div class="rows">
			{#each apps as app (app.id)}
				{#if app.state === 'awaiting_approval'}
				<div class="item">
					<SettingsRow name={app.name} subTone={tone(app)} stackActions>
						{#snippet sub()}{@render secondLine(app)}{#if app.notice}<span class="line">{app.notice}</span>{/if}{/snippet}
						{#snippet actions()}
							{#if needsPlace(app)}
								<Button variant="primary" size="sm" onclick={() => { whereApprove = true; whereApp = app; }}>Choose where it works…</Button>
							{:else}
								<Button variant="primary" size="sm" loading={actionId === app.id} onclick={() => run(app, 'approve')}>Approve</Button>
							{/if}
							<Button variant="ghost" size="sm" loading={actionId === app.id} onclick={() => run(app, 'decline')}>Decline</Button>
						{/snippet}
					</SettingsRow>
					{#if errorId === app.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
					{#if hasSignInToShow(app)}
						<div class="details"><AppSignInPanel {app} {signInHere} confirmOut={confirmingOut === app.id} onConfirmOut={(on) => confirmingOut = on ? app.id : null} onChange={(patch) => replace({ ...app, ...patch })} onRefresh={refresh}
							onSignedOut={() => signedOut = new Set(signedOut).add(app.id)} /></div>
					{/if}
				</div>
				{:else}
				<div class="item">
					<SettingsRow name={app.name} subTone={tone(app)} chevron expanded={isOpen(app)} onOpen={() => openId = openId === app.id ? null : app.id}>
						{#snippet sub()}{@render secondLine(app)}{/snippet}
					</SettingsRow>
					{#if isOpen(app)}
						<div class="details">
							{#if errorId === app.id && actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
							<AppSignInPanel {app} {signInHere} confirmOut={confirmingOut === app.id} onConfirmOut={(on) => confirmingOut = on ? app.id : null} onChange={(patch) => replace({ ...app, ...patch })} onRefresh={refresh}
								onSignedOut={() => signedOut = new Set(signedOut).add(app.id)} autoStart={signInAfterApprove === app.id} onAutoStarted={() => signInAfterApprove = null} />
							<div class="actions">
								{#if app.state === 'expired'}
									<!-- A request that ran out can't be placed, reconnected or approved; Dismiss runs Decline's cleanup (copy sheet). -->
									<Button variant="ghost" size="sm" loading={actionId === app.id} onclick={() => run(app, 'decline')}>Dismiss</Button>
								{:else}
									<Button variant="ghost" size="sm" onclick={() => { whereApprove = false; whereApp = app; }}>Where it works…</Button>
									<Button variant="ghost" size="sm" loading={actionId === app.id} onclick={() => run(app, 'reconnect')}>Reconnect</Button>
									{#if app.sign_in?.signed_in && confirmingOut !== app.id}<Button variant="ghost" size="sm" onclick={() => confirmingOut = app.id}>Sign out</Button>{/if}
									<span class="danger-text"><Button variant="ghost" size="sm" loading={actionId === app.id} onclick={() => run(app, 'remove')}>Remove</Button></span>
								{/if}
							</div>
						</div>
					{/if}
				</div>
				{/if}
			{/each}
		</div>
	{/if}
</section>

{#if whereApp}<WhereItWorks app={whereApp} approve={whereApprove} onClose={() => whereApp = null} onSaved={(app) => { if (whereApprove) approved(app); else replace(app); whereApp = null; }} />{/if}
{#if connectAppOpen}<ConnectAppFlow onClose={() => connectAppOpen = false} onConnected={(app) => { apps = [...apps, app]; }} />{/if}

<style>
	.connected-apps { width: 100%; scroll-margin-top: var(--space-5); }
	.heading { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
	/* The ledes and the empty, loading and error states are one body size, inset to the edge the page title and rows use. */
	.lede, .state { margin: 0 0 var(--space-2); padding: 0 var(--space-2); }
	.lede { color: var(--text-tertiary); font-size: var(--text-caption); line-height: 1.45; }
	.state { color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; }
	.state { margin-top: var(--space-5); display: grid; gap: var(--space-3); justify-items: start; }
	.state p { margin: 0; }
	.rows { display: flex; flex-direction: column; gap: var(--space-1); margin-top: var(--space-4); }
	/* The second line is two-toned: the row's state takes its status colour, the place stays quiet. */
	.place, .line { color: var(--text-tertiary); }
	.line { display: block; }
	.details { padding: var(--space-1) var(--space-2) var(--space-3); }
	.actions { display: flex; flex-wrap: wrap; gap: var(--space-1); margin-left: calc(-1 * var(--button-pad-x-sm)); }
	/* Remove is a text action in the danger tone. */
	.danger-text :global(.btn.ghost), .danger-text :global(.btn.ghost:hover:not(:disabled)) { color: var(--red-text); }
	.error { color: var(--red); margin: 0 0 var(--space-2); padding: 0 var(--space-2); font-size: var(--text-footnote); }
	@media (max-width: 768px) { .heading { align-items: stretch; flex-direction: column; } .heading :global(.btn) { align-self: flex-start; } }
</style>
