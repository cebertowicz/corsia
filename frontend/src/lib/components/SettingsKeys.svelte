<script lang="ts">
	import { onMount } from 'svelte';
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
	import { requestProviderCard, requestServerCard } from '$lib/stores/settingsNavigation';

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
			open = null;
			delete checkResults[name];
			await load();
		} catch (error) {
			formError = error instanceof KeyWriteError ? error.message : KEYS_NOT_SAVED;
		} finally {
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
	<div class="settings-card" data-key-entry={entry.name}>
		<h3>{entry.label}</h3>
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

		{#if open?.name === entry.name}
			{@render keyForm()}
		{:else}
			{#if checkResults[entry.name]}
				<Message variant={checkResults[entry.name].ok ? 'ok' : 'err'}>{checkResults[entry.name].text}</Message>
			{/if}
			<div class="card-actions">
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
	.key-meta { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: var(--text-footnote); color: var(--text-secondary); }
	.key-status { color: var(--text-primary); }
	.key-form { margin-top: 12px; }
	.card-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 16px; }

	.provider-keys { padding: 8px 24px; }
	.provider-row { padding: 10px 0; }
	.provider-row + .provider-row { border-top: 1px solid var(--border-subtle); }
	.row-line { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; font-size: var(--text-subhead); color: var(--text-primary); }
	.row-label { font-weight: 600; }
	.row-sep, .provider-row .key-status { color: var(--text-secondary); }
	.row-actions { display: flex; gap: 8px; margin-left: auto; }
	.row-note { font-size: var(--text-footnote); color: var(--text-muted); margin: 4px 0 0; line-height: 1.5; }
</style>
