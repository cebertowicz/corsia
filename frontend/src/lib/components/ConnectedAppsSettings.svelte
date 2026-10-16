<script lang="ts">
	import { onMount } from 'svelte';
	import { Button } from '$lib/components/ui';
	import {
		approveConnectedApp, declineConnectedApp, loadConnectedApps, reconnectConnectedApp, removeConnectedApp,
		type ConnectedApp,
	} from '$lib/api/connectedApps';
	import WhereItWorks from './WhereItWorks.svelte';

	let apps = $state<ConnectedApp[]>([]);
	let loading = $state(true);
	let error = $state('');
	let actionId = $state<string | null>(null);
	let actionError = $state('');
	let whereApp = $state<ConnectedApp | null>(null);
	let openId = $state<string | null>(null);

	function stateLine(app: ConnectedApp): string {
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

	function placeLine(app: ConnectedApp): string {
		return app.reach.mode === 'all' ? 'All projects' : 'Select individually';
	}

	async function load(): Promise<void> {
		loading = true;
		error = '';
		try { apps = await loadConnectedApps(); }
		catch (cause) { error = cause instanceof Error ? cause.message : 'Could not load connected apps.'; }
		finally { loading = false; }
	}

	async function run(app: ConnectedApp, action: 'approve' | 'decline' | 'reconnect' | 'remove'): Promise<void> {
		actionId = app.id;
		actionError = '';
		try {
			if (action === 'approve') replace(await approveConnectedApp(app.id));
			else if (action === 'decline') replace(await declineConnectedApp(app.id));
			else if (action === 'reconnect') replace(await reconnectConnectedApp(app.id));
			else { await removeConnectedApp(app.id); apps = apps.filter((row) => row.id !== app.id); }
		} catch (cause) {
			actionError = cause instanceof Error ? cause.message : 'Could not update this app. Try again.';
		} finally { actionId = null; }
	}

	function replace(next: ConnectedApp): void { apps = apps.map((app) => app.id === next.id ? { ...app, ...next } : app); }
	onMount(load);
</script>

<section class="connected-apps" data-settings-anchor="connected-apps" id="connected-apps">
	<div class="heading"><div><h2>Connected apps</h2><p>Connect the apps you already use, like GitHub or Notion, so your agents can work in them. Apps here work in every project. A project can turn one off, or connect apps of its own, in its settings.</p></div></div>
	{#if loading}
		<p class="state" role="status">Loading connected apps…</p>
	{:else if error}
		<div class="state" role="alert"><p>{error}</p><Button onclick={load}>Try again</Button></div>
	{:else if apps.length === 0}
		<p class="state">No apps shared with every project yet. Connect one here, or ask CORSIA in chat to bring over the apps you use in Claude Code, Claude Desktop, Cursor or Codex, for example: “Bring over my apps from Claude.”</p>
	{:else}
		<div class="rows">
			{#each apps as app (app.id)}
				<article class:open={openId === app.id || app.state === 'awaiting_approval'} class="app-row">
					<button class="summary press-tint" aria-expanded={openId === app.id || app.state === 'awaiting_approval'} onclick={() => openId = openId === app.id ? null : app.id}>
						<span class="app-copy"><strong title={app.name}>{app.name}</strong><span>{stateLine(app)} · {placeLine(app)}</span></span><span aria-hidden="true">›</span>
					</button>
					{#if openId === app.id || app.state === 'awaiting_approval'}
						<div class="details">
							{#if actionError && actionId === null}<p class="error" role="alert">{actionError}</p>{/if}
							<div class="actions">
								{#if app.state === 'awaiting_approval'}
									<Button variant="primary" loading={actionId === app.id} onclick={() => run(app, 'approve')}>Approve</Button>
									<Button loading={actionId === app.id} onclick={() => run(app, 'decline')}>Decline</Button>
								{:else}
									<Button onclick={() => whereApp = app}>Where it works…</Button>
									<Button loading={actionId === app.id} onclick={() => run(app, 'reconnect')}>Reconnect</Button>
									<Button variant="danger" loading={actionId === app.id} onclick={() => run(app, 'remove')}>Remove</Button>
								{/if}
							</div>
						</div>
					{/if}
				</article>
			{/each}
		</div>
	{/if}
</section>

{#if whereApp}<WhereItWorks app={whereApp} onClose={() => whereApp = null} onSaved={(app) => { replace(app); whereApp = null; }} />{/if}

<style>
	.connected-apps { max-width: 760px; margin-top: var(--space-7); scroll-margin-top: var(--space-5); }
	.heading h2 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); }
	.heading p { margin: var(--space-2) 0 0; color: var(--text-secondary); line-height: 1.5; }
	.rows { display: grid; gap: var(--space-2); margin-top: var(--space-5); }
	.app-row { border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-secondary); overflow: hidden; }
	.summary { appearance: none; border: 0; background: none; color: inherit; width: 100%; padding: var(--space-4); display: flex; justify-content: space-between; align-items: center; cursor: pointer; text-align: left; }
	.app-copy { min-width: 0; display: grid; gap: var(--space-1); }
	.app-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
	.app-copy span, .state { color: var(--text-secondary); font-size: var(--text-footnote); }
	.details { border-top: 1px solid var(--border-subtle); padding: var(--space-3) var(--space-4); }
	.actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
	.error { color: var(--red); margin: 0 0 var(--space-2); font-size: var(--text-footnote); }
	.state { margin: var(--space-5) 0; display: grid; gap: var(--space-3); }
	@media (max-width: 768px) { .connected-apps { margin-top: var(--space-5); } }
</style>
