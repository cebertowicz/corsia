<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { Button, Field, Message } from '$lib/components/ui';
	import {
		checkKey,
		clearKeyValue,
		KEYS_NOT_SAVED,
		KeyWriteError,
		ignoredEnvironmentFact,
		ignoredEnvironmentNote,
		listKeys,
		removeKey,
		saveKeyValue,
		type CheckResult,
		type KeyEntry,
		type ProviderKeyRow,
		type ServerKeyRow,
	} from '$lib/stores/keys';
	import { requestProviderCard, requestServerCard, settingsAnchor, settingsNavigation } from '$lib/stores/settingsNavigation';
	import { connectedAppStateLine, loadConnectedAppsList } from '$lib/api/connectedApps';
	import { ms } from '$lib/popoverMotion';

	type Action = 'set' | 'clear' | 'remove';

	let entries = $state<KeyEntry[]>([]);
	let providerRows = $state<ProviderKeyRow[]>([]);
	let serverRows = $state<ServerKeyRow[]>([]);
	let loadError = $state('');
	let open = $state<{ name: string; action: Action } | null>(null);
	// Both fields are write-only: emptied after every submit and never restored.
	let keyValue = $state('');
	let password = $state('');
	let busy = $state(false);
	let formError = $state('');
	let checking = $state<string | null>(null);
	let checkResults = $state<Record<string, { ok: boolean; text: string }>>({});
	let reconnectStatus = $state('');
	let arrivedFor: string | null = null;
	let arrivalEntry = $state<string | null>(null);
	const connectedAppKey = $derived($settingsNavigation.connectedAppKey);
	const RECONNECT_TIMEOUT_MS = 30_000;
	const RECONNECT_RETRY_MS = 1_000;

	const FAILURE_REASONS: Record<string, string> = {
		network: "couldn't reach the search service",
		timeout: 'the search service took too long',
		'rate limit or quota': 'this key has hit its usage limit',
		'provider error': 'the search service returned an error',
	};

	async function load() {
		try {
			const listing = await listKeys();
			entries = listing.keys;
			providerRows = listing.providerKeys;
			serverRows = listing.serverKeys;
			loadError = '';
		} catch {
			loadError = KEYS_NOT_SAVED;
		}
	}

	onMount(load);

	/** The target action pulses for two hero-plus tokens (1.2s with the current token), then retains keyboard focus in its normal state. */
	$effect(() => {
		const target = connectedAppKey;
		if (!target) {
			arrivedFor = null;
			arrivalEntry = null;
			return;
		}
		if (!entries.some((entry) => entry.name === target.entry) || arrivedFor === target.entry || open?.name === target.entry) return;
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout> | undefined;
		arrivedFor = target.entry;
		arrivalEntry = target.entry;
		void tick().then(() => {
			if (cancelled) return;
			const card = document.querySelector<HTMLElement>(`[data-key-entry="${target.entry}"]`);
			const action = card?.querySelector<HTMLButtonElement>('.card-actions .btn.primary');
			if (!action) return;
			action.focus({ preventScroll: true });
			timer = setTimeout(() => {
				if (arrivalEntry === target.entry) arrivalEntry = null;
			}, ms('--dur-hero-plus') * 2);
		});
		return () => { cancelled = true; if (timer) clearTimeout(timer); if (arrivalEntry === target.entry) arrivalEntry = null; };
	});

	function cardLabel(entry: KeyEntry): string {
		const ref = connectedAppKey?.refs.find((candidate) => candidate.entry === entry.name);
		if (!connectedAppKey || !ref) return entry.label;
		return connectedAppKey.refs.length > 1 ? `${connectedAppKey.app} key: ${ref.name}` : `${connectedAppKey.app} key`;
	}

	function status(entry: KeyEntry): string {
		if (!entry.is_set) return 'Not set';
		if (!entry.set_at) return 'Set';
		const date = new Date(entry.set_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
		return `Set ${date}`;
	}

	function openForm(name: string, action: Action) {
		open = { name, action };
		keyValue = '';
		password = '';
		formError = '';
	}

	function closeForm() {
		open = null;
		keyValue = '';
		password = '';
		formError = '';
	}

	async function reconnectAfterKeySave(appName: string) {
		const deadline = Date.now() + RECONNECT_TIMEOUT_MS;
		try {
			while (true) {
				const app = (await loadConnectedAppsList()).servers.find((candidate) => candidate.name === appName);
				if (!app) break;
				if (app.state === 'working') {
					reconnectStatus = `${appName} is connected.`;
					return;
				}
				if (app.state === 'token_rejected') {
					reconnectStatus = `${appName} didn't accept this key. Create a new one in your ${appName} settings, then press Replace.`;
					return;
				}
				if (app.state !== 'connecting' && app.state !== 'awaiting_approval') {
					reconnectStatus = connectedAppStateLine(app);
					return;
				}
				if (Date.now() >= deadline) break;
				await new Promise<void>((resolve) => setTimeout(resolve, RECONNECT_RETRY_MS));
			}
		} catch {
			// The key already saved; do not turn a reconnect lookup failure into a failed save.
		}
		reconnectStatus = 'Saved.';
	}

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (!open || busy) return;
		const { name, action } = open;
		if (action === 'set' && !keyValue.trim()) {
			formError = 'Paste the key first.';
			return;
		}
		if (!password) {
			formError = 'Enter your CORSIA password.';
			return;
		}
		const value = keyValue;
		const typed = password;
		keyValue = '';
		password = '';
		busy = true;
		formError = '';
		try {
			if (action === 'set') await saveKeyValue(name, value, typed);
			else if (action === 'clear') await clearKeyValue(name, typed);
			else await removeKey(name, typed);
		} catch (error) {
			formError = error instanceof KeyWriteError ? error.message : KEYS_NOT_SAVED;
			busy = false;
			return;
		}
		open = null;
		delete checkResults[name];
		try {
			if (action === 'set' && connectedAppKey?.entry === name) {
				reconnectStatus = `Saved. Reconnecting ${connectedAppKey.app}…`;
				await tick();
				await reconnectAfterKeySave(connectedAppKey.app);
			}
		} finally {
			await load();
			busy = false;
		}
	}

	function checkText(result: CheckResult): { ok: boolean; text: string } {
		if (result.outcome === 'works') return { ok: true, text: 'Web search works.' };
		if (result.outcome === 'key_refused') {
			return { ok: false, text: 'The key was refused. Check it and save it again.' };
		}
		const reason = FAILURE_REASONS[result.reason ?? ''] ?? FAILURE_REASONS['provider error'];
		return { ok: false, text: `Web search didn't work just now: ${reason}.` };
	}

	async function check(name: string) {
		checking = name;
		try {
			checkResults[name] = checkText(await checkKey(name));
		} catch (error) {
			checkResults[name] = {
				ok: false,
				text: error instanceof KeyWriteError ? error.message : FAILURE_REASONS['provider error'],
			};
		} finally {
			checking = null;
		}
	}
</script>

<div class="section-header" id="settings-keys">
	<h2>Keys</h2>
	<p>Agents can use these keys but can never see them. They're stored on this Mac, and a backup of CORSIA's state folder includes them.</p>
</div>

{#if loadError}
	<Message variant="err">{loadError}</Message>
{/if}

{#snippet keyForm()}
	{#if open}
	<form class="key-form" onsubmit={submit}>
		{#if open.action === 'set'}
			<Field label="Key">
				<input
					type="password"
					aria-label="Key"
					autocomplete="off"
					data-1p-ignore
					data-lpignore="true"
					data-bwignore
					data-form-type="other"
					spellcheck="false"
					bind:value={keyValue}
				/>
			</Field>
		{/if}
		<Field label="CORSIA password">
			<input type="password" aria-label="CORSIA password" autocomplete="current-password" bind:value={password} />
		</Field>
		{#if formError}
			<Message variant="err">{formError}</Message>
		{/if}
		<div class="card-actions">
			<Button type="submit" variant={open.action === 'set' ? 'primary' : 'danger'} loading={busy}>
				{open.action === 'set' ? 'Save' : open.action === 'clear' ? 'Clear' : 'Remove'}
			</Button>
			<Button variant="ghost" disabled={busy} onclick={closeForm}>Cancel</Button>
		</div>
	</form>
	{/if}
{/snippet}

{#each entries as entry (entry.name)}
	<div class="settings-card" data-key-entry={entry.name} data-settings-anchor={`key-${entry.name}`} use:settingsAnchor={`key-${entry.name}`}>
		<h3>{cardLabel(entry)}</h3>
		{#if entry.description}
			<p class="card-desc">{entry.description}</p>
		{/if}
		<div class="key-meta">
			{#if entry.used_by}
				<span>Used by: {entry.used_by}</span>
			{/if}
			{#if entry.added_by}
				<span>Added by {entry.added_by}</span>
			{/if}
			<span class="key-status">{status(entry)}</span>
		</div>
		{#if entry.ignored_env_var}
			<p class="row-note" data-ignored-env>{ignoredEnvironmentNote(entry.ignored_env_var)}</p>
		{/if}
		{#if connectedAppKey?.entry === entry.name && reconnectStatus}<p class="reconnect-status" role="status">{reconnectStatus}</p>{/if}

		{#if open?.name === entry.name}
			{@render keyForm()}
		{:else}
			{#if checkResults[entry.name]}
				<Message variant={checkResults[entry.name].ok ? 'ok' : 'err'}>{checkResults[entry.name].text}</Message>
			{/if}
			<div class="card-actions" class:arrival={arrivalEntry === entry.name}>
				<Button variant="primary" onclick={() => openForm(entry.name, 'set')}>
					{entry.is_set ? 'Replace' : 'Set'}
				</Button>
				{#if entry.is_set}
					<Button onclick={() => openForm(entry.name, 'clear')}>Clear</Button>
				{/if}
				{#if entry.checkable && entry.is_set}
					<Button loading={checking === entry.name} onclick={() => check(entry.name)}>Check</Button>
				{/if}
				{#if !entry.builtin}
					<Button variant="danger" onclick={() => openForm(entry.name, 'remove')}>Remove</Button>
				{/if}
			</div>
		{/if}
	</div>
{/each}

{#if providerRows.length || serverRows.length}
	<h3 class="subhead">Provider keys</h3>
	<div class="settings-card provider-keys">
		{#each providerRows as row (row.name)}
			<div class="provider-row" data-provider-key={row.provider}>
				<div class="row-line">
					<span class="row-label">{row.label}</span>
					<span class="row-sep" aria-hidden="true">·</span>
					<span class="key-status">{row.is_set ? 'Set' : 'Not set'}</span>
					<span class="row-actions">
						{#if !row.unsupported}
							<Button variant="ghost" size="sm" onclick={() => requestProviderCard(row.provider)}>
								{row.is_set ? 'Change in Providers' : 'Add in Providers'}
							</Button>
						{:else if row.is_set && open?.name !== row.name}
							<Button variant="danger" size="sm" onclick={() => openForm(row.name, 'remove')}>Remove</Button>
						{/if}
					</span>
				</div>
				{#if row.ignored_env_var}
					<p class="row-note" data-ignored-env>{ignoredEnvironmentFact(row.ignored_env_var)}</p>
				{/if}
				{#if open?.name === row.name}
					{@render keyForm()}
				{/if}
			</div>
		{/each}
		{#each serverRows as row (row.name)}
			<div class="provider-row" data-server-key={row.server_id}>
				<div class="row-line">
					<span class="row-label">{row.label}</span>
					<span class="row-sep" aria-hidden="true">·</span>
					<span class="key-status">{row.is_set ? 'Set' : 'Not set'}</span>
					<span class="row-actions">
						<Button variant="ghost" size="sm" onclick={() => requestServerCard(row.server_id)}>
							{row.is_set ? 'Change in Providers' : 'Add in Providers'}
						</Button>
					</span>
				</div>
			</div>
		{/each}
	</div>
{/if}

<style>
	.section-header { margin-bottom: 24px; }
	.section-header h2 { font-size: var(--text-title2); font-weight: 600; color: var(--text-primary); margin: 0 0 4px; }
	.section-header p { font-size: var(--text-subhead); color: var(--text-muted); margin: 0; }

	.subhead { font-size: var(--text-callout); font-weight: 600; color: var(--text-primary); margin: 24px 0 12px; }

	.settings-card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 20px 24px; margin-bottom: 16px; }
	.settings-card h3 { font-size: var(--text-callout); font-weight: 600; color: var(--text-primary); margin: 0 0 4px; }
	.card-desc { font-size: var(--text-subhead); color: var(--text-muted); margin: 0 0 8px; line-height: 1.5; }
	.reconnect-status { color: var(--text-secondary); font-size: var(--text-subhead); line-height: 1.5; margin: var(--space-2) 0 0; }
	.key-meta { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: var(--text-footnote); color: var(--text-secondary); }
	.key-status { color: var(--text-primary); }
	.key-form { margin-top: 12px; }
	.card-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 16px; }
	.card-actions.arrival :global(.btn.primary) { animation: key-action-arrival calc(var(--dur-hero-plus) * 2) var(--ease-entrance); }
	@keyframes key-action-arrival {
		0% { opacity: 0.72; transform: scale(0.96); }
		20% { opacity: 1; transform: scale(1.04); }
		45%, 100% { opacity: 1; transform: scale(1); }
	}
	@media (prefers-reduced-motion: reduce) { .card-actions.arrival :global(.btn.primary) { animation: none; } }

	.provider-keys { padding: 8px 24px; }
	.provider-row { padding: 10px 0; }
	.provider-row + .provider-row { border-top: 1px solid var(--border-subtle); }
	.row-line { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; font-size: var(--text-subhead); color: var(--text-primary); }
	.row-label { font-weight: 600; }
	.row-sep, .provider-row .key-status { color: var(--text-secondary); }
	.row-actions { display: flex; gap: 8px; margin-left: auto; }
	.row-note { font-size: var(--text-footnote); color: var(--text-muted); margin: 4px 0 0; line-height: 1.5; }
</style>
