<script lang="ts">
	import { onMount } from 'svelte';
	import { Button, Field } from '$lib/components/ui';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsRow from './SettingsRow.svelte';
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

	function entryLine(entry: KeyEntry): string {
		return [
			status(entry),
			entry.description,
			entry.used_by ? `Used by: ${entry.used_by}` : '',
			entry.added_by ? `Added by ${entry.added_by}` : '',
		].filter(Boolean).join(' · ');
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

{#if loadError}
	<p class="note failed" role="alert">{loadError}</p>
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
			<p class="note failed" role="alert">{formError}</p>
		{/if}
		<div class="form-actions">
			<Button type="submit" variant={open.action === 'set' ? 'primary' : 'danger'} loading={busy}>
				{open.action === 'set' ? 'Save' : open.action === 'clear' ? 'Clear' : 'Remove'}
			</Button>
			<Button variant="ghost" disabled={busy} onclick={closeForm}>Cancel</Button>
		</div>
	</form>
	{/if}
{/snippet}

{#if entries.length}
	<SettingsGroup label="Service keys" hint="Agents can use these keys but can never see them. They're stored on this Mac, and a backup of CORSIA's state folder includes them.">
		{#each entries as entry (entry.name)}
			<div class="entry" data-key-entry={entry.name}>
				<SettingsRow name={entry.label} sub={entryLine(entry)} note={entry.ignored_env_var ? ignoredEnvironmentNote(entry.ignored_env_var) : ''} wrap stackActions>
					{#snippet actions()}
						{#if open?.name !== entry.name}
							<Button variant="ghost" size="sm" onclick={() => openForm(entry.name, 'set')}>
								{entry.is_set ? 'Replace' : 'Set'}
							</Button>
							{#if entry.is_set}
								<Button variant="ghost" size="sm" onclick={() => openForm(entry.name, 'clear')}>Clear</Button>
							{/if}
							{#if entry.checkable && entry.is_set}
								<Button variant="ghost" size="sm" loading={checking === entry.name} onclick={() => check(entry.name)}>Check</Button>
							{/if}
							{#if !entry.builtin}
								<Button variant="ghost" size="sm" onclick={() => openForm(entry.name, 'remove')}>Remove</Button>
							{/if}
						{/if}
					{/snippet}
				</SettingsRow>
				{#if open?.name === entry.name}
					{@render keyForm()}
				{:else if checkResults[entry.name]}
					<p class="note" class:ok={checkResults[entry.name].ok} class:failed={!checkResults[entry.name].ok} role="status">{checkResults[entry.name].text}</p>
				{/if}
			</div>
		{/each}
	</SettingsGroup>
{/if}

{#if providerRows.length || serverRows.length}
	<SettingsGroup label="Provider keys">
		{#each providerRows as row (row.name)}
			<div class="entry" data-provider-key={row.provider}>
				<SettingsRow name={row.label} sub={row.is_set ? 'Set' : 'Not set'} note={row.ignored_env_var ? ignoredEnvironmentFact(row.ignored_env_var) : ''}>
					{#snippet actions()}
						{#if !row.unsupported}
							<Button variant="ghost" size="sm" onclick={() => requestProviderCard(row.provider)}>
								{row.is_set ? 'Change in Providers' : 'Add in Providers'}
							</Button>
						{:else if row.is_set && open?.name !== row.name}
							<Button variant="ghost" size="sm" onclick={() => openForm(row.name, 'remove')}>Remove</Button>
						{/if}
					{/snippet}
				</SettingsRow>
				{#if open?.name === row.name}
					{@render keyForm()}
				{/if}
			</div>
		{/each}
		{#each serverRows as row (row.name)}
			<div class="entry" data-server-key={row.server_id}>
				<SettingsRow name={row.label} sub={row.is_set ? 'Set' : 'Not set'}>
					{#snippet actions()}
						<Button variant="ghost" size="sm" onclick={() => requestServerCard(row.server_id)}>
							{row.is_set ? 'Change in Providers' : 'Add in Providers'}
						</Button>
					{/snippet}
				</SettingsRow>
			</div>
		{/each}
	</SettingsGroup>
{/if}

<style>
	.entry { display: flex; flex-direction: column; }
	/* Verbs are the row's trailing actions; results and forms sit under it at its inset, results as text in the status colours (§0.1). */
	.note { margin: 0 0 var(--space-1); padding: 0 var(--space-2); font-size: var(--text-caption); line-height: 1.45; color: var(--text-tertiary); }
	.note.ok { color: var(--green-text); }
	.note.failed { color: var(--red-text); }
	.key-form { padding: 0 var(--space-2) var(--space-2); }
	.form-actions { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; margin-top: var(--space-3); }
</style>
