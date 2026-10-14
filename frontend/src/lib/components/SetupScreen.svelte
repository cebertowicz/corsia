<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { Button, Field, ListRow, Message, StatePill, Toggle } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from './SetupShell.svelte';
	import { token, user } from '$lib/stores/auth';
	import { fetchSources, recheckSources, sourceIsReady, type ModelSource } from '$lib/conductorSources';
	import { accountToSignIn, startSignin } from '$lib/conductorSourcesApi';
	import { updateProvider, loadProviders, providers } from '$lib/stores/providers';

	interface Props { onExistingAccount: () => void; onAccountCreated: () => void; onFinished: () => void; }
	const { onExistingAccount, onAccountCreated, onFinished }: Props = $props();
	type Page = 'checking' | 'welcome' | 'account' | 'models' | 'hard-stop' | 'error';
	type SetupStatus = { has_users?: boolean; apple_silicon?: boolean; macos_supported?: boolean; ram_tier?: 'eligible' | 'low' | 'below_minimum'; free_disk_gb?: number; local_eligible?: boolean; app_location?: 'applications' | 'disk_image' | 'other'; work_folder?: string; model_name?: string; model_size_gb?: number; error?: string };
	let page = $state<Page>('checking');
	let status = $state<SetupStatus>({});
	let localWanted = $state(true);
	let statusError = $state('');
	let name = $state('');
	let username = $state('');
	let password = $state('');
	let confirmation = $state('');
	let email = $state('');
	let folder = $state('~/CORSIA');
	let accountBusy = $state(false);
	let accountError = $state('');
	let confirmationError = $state('');
	let sources = $state<ModelSource[]>([]);
	let sourceError = $state('');
	let keyOpen = $state(false);
	let providerName = $state('anthropic');
	let apiKey = $state('');
	let savingKey = $state(false);

	const steps = $derived<SetupStep[]>([
		{ label: 'Welcome', icon: 'check', state: page === 'welcome' || page === 'hard-stop' || page === 'checking' ? 'current' : 'done' },
		{ label: 'Your account', icon: 'person', state: page === 'account' ? 'current' : page === 'models' ? 'done' : 'todo' },
		{ label: 'AI model', icon: 'chip', state: page === 'models' ? 'current' : 'todo' },
	]);
	const local = $derived(sources.find((source) => source.source === 'omlx'));
	const localProgress = $derived(local?.download_percent == null ? undefined : local.download_percent / 100);
	const jobs = $derived<SetupJob[]>([
		...(local?.setup_downloading || local?.setup_stage ? [{ label: status.model_name || local.name || 'Local model', state: 'busy' as const, meta: local.download_percent == null ? 'Downloading' : `${local.download_percent}%`, progress: localProgress }] : []),
		...(page === 'account' && accountBusy ? [{ label: 'Starting CORSIA', state: 'busy' as const, meta: 'Preparing your account' }] : []),
		...(page === 'error' ? [{ label: 'Starting CORSIA', state: 'error' as const, meta: 'Couldn’t start' }] : []),
		...(page === 'models' ? [{ label: 'Starting CORSIA', state: 'done' as const, meta: 'Ready' }] : []),
	]);
	const anyReady = $derived(sources.some(sourceIsReady));
	const localReady = $derived(local?.state === 'ready');
	const canFinish = $derived(anyReady || localReady);
	const modelSize = $derived(status.model_size_gb ? `${status.model_size_gb} GB` : 'a local model');
	const downloadModelLabel = $derived(status.model_name || 'local model');
	const usernameError = $derived(username && !/^[a-z0-9_-]{1,32}$/.test(username) ? "Use 1–32 lowercase letters, numbers, '-' or '_'." : '');

	async function readStatus() {
		statusError = '';
		page = 'checking';
		try {
			const response = await fetch('/api/setup/status');
			if (response.status === 409) { onExistingAccount(); return; }
			if (!response.ok) throw new Error(`setup status ${response.status}`);
			status = await response.json();
			folder = status.work_folder || folder;
			if (status.has_users) { onExistingAccount(); return; }
			if (status.apple_silicon === false || status.macos_supported === false || status.app_location === 'disk_image') page = 'hard-stop';
			else {
				localWanted = status.local_eligible !== false
					&& (status.free_disk_gb ?? Infinity) >= (status.model_size_gb ?? 0);
				page = 'welcome';
			}
		} catch {
			statusError = 'CORSIA couldn’t check this computer. Try again, or sign in if you already set it up.';
			page = 'error';
		}
	}

	async function startLocalDownload() {
		if (!localWanted) return;
		const response = await fetch('/api/setup/local-download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
		if (!response.ok && response.status !== 409) throw new Error('download failed');
	}

	async function beginSetup() {
		try { await startLocalDownload(); page = 'account'; }
		catch { statusError = 'The local model download couldn’t start. You can continue without it and sign in to another model.'; page = 'account'; }
	}

	function validateConfirmation() {
		confirmationError = confirmation && password !== confirmation ? 'Passwords don’t match.' : '';
	}

	async function createAccount() {
		validateConfirmation();
		if (!name.trim() || !username.trim() || usernameError || password.length < 8 || confirmationError || accountBusy) return;
		accountBusy = true; accountError = '';
		try {
			const response = await fetch('/api/setup/first-account', {
				method: 'POST', headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ name: name.trim(), username: username.trim(), password, email: email.trim() || null }),
			});
			if (response.status === 409) { onExistingAccount(); return; }
			if (response.status === 422) {
				const detail = (await response.json().catch(() => null))?.detail;
				accountError = typeof detail === 'string' ? detail : 'Check the highlighted account details and try again.';
				return;
			}
			if (!response.ok) throw new Error(`account ${response.status}`);
			const result = await response.json();
			onAccountCreated();
			token.set(result.token); user.set(result.user ?? { id: result.user_id, name: name.trim() });
			await refreshSources();
			page = 'models';
		} catch { accountError = 'CORSIA couldn’t start. Try again.'; page = 'error'; }
		finally { accountBusy = false; password = ''; confirmation = ''; }
	}

	async function refreshSources() {
		const current = get(token);
		if (!current) return;
		try { sources = await recheckSources(current); sourceError = ''; }
		catch { try { sources = await fetchSources(current); sourceError = ''; } catch { sourceError = 'CORSIA couldn’t check your model sources. Try again.'; } }
	}

	async function signIn(source: ModelSource) {
		const current = get(token);
		if (!current) return;
		try {
			const conductor = await fetch('/api/conductor', { headers: { Authorization: `Bearer ${current}` } });
			const target = (await conductor.json())?.target?.thread_id;
			if (!target) throw new Error('conductor unavailable');
			const accountId = await accountToSignIn(current, source.source, source.name);
			await startSignin(current, accountId, target);
			await refreshSources();
		} catch { sourceError = `CORSIA couldn’t start ${source.name} sign-in. Try again.`; }
	}

	async function saveApiKey() {
		if (!apiKey.trim() || savingKey) return;
		savingKey = true; sourceError = '';
		try {
			const result = await updateProvider(providerName, { api_key: apiKey });
			if (!result.ok) throw new Error(result.error);
			apiKey = ''; keyOpen = false; await loadProviders(); await refreshSources();
		} catch { sourceError = 'CORSIA couldn’t save that API key. Check it and try again.'; }
		finally { savingKey = false; }
	}

	onMount(readStatus);
</script>

{#if page === 'checking'}
	<SetupShell {steps} title="Checking this computer" lede="This takes a moment." />
{:else if page === 'hard-stop'}
	<SetupShell {steps} title={status.apple_silicon === false ? 'CORSIA needs Apple silicon' : status.macos_supported === false ? 'Update macOS to continue' : 'Move CORSIA to Applications'} lede={status.apple_silicon === false ? 'This version of CORSIA runs on Macs with Apple silicon.' : status.macos_supported === false ? 'Update to macOS 15 or later, then open CORSIA again.' : 'Move CORSIA to Applications, then open it again so updates work.'}>
		{#snippet secondary()}<Button variant="chip" onclick={readStatus}>Check again</Button>{/snippet}
	</SetupShell>
{:else if page === 'welcome'}
	<SetupShell {steps} title="Welcome to CORSIA" mark lede="CORSIA runs AI agents that work on your projects with you." primary={{ label: 'Get started' }} onPrimary={beginSetup}>
		<div class="welcome-copy">
			{#if status.local_eligible === false}
				<p>This computer can’t run a local model. You can connect Claude Code, Codex, or an API key in the next step.</p>
			{:else if (status.free_disk_gb ?? Infinity) < (status.model_size_gb ?? 0)}
				<p>Needs {modelSize} free; {status.free_disk_gb} GB is free.</p>
				<Button variant="chip" onclick={readStatus}>Free up space, then check again</Button>
			{:else}
				<div class="download-choice">
					<div class="switch"><Toggle checked={localWanted} onchange={(next) => localWanted = next} ariaLabel={`Download ${downloadModelLabel} now`} /><span>Download {downloadModelLabel} now</span></div>
					<span class="local-meta">{modelSize} · runs on this computer</span>
				</div>
			{/if}
		</div>
	</SetupShell>
{:else if page === 'account'}
	<SetupShell {steps} {jobs} title="Create your account" primary={{ label: 'Continue', disabled: accountBusy || !name.trim() || !username.trim() || !!usernameError || password.length < 8 || !!confirmationError, loading: accountBusy }} onPrimary={createAccount}>
		<form class="form" onsubmit={(event) => { event.preventDefault(); createAccount(); }}>
			<Field variant="chip" layout="stacked" label="Name"><input autocomplete="name" bind:value={name} /></Field>
			<Field variant="chip" layout="stacked" label="Username" error={usernameError || undefined}><input autocomplete="username" autocapitalize="off" bind:value={username} /></Field>
			<Field variant="chip" layout="stacked" label="Password" error={password && password.length < 8 ? 'Use at least 8 characters.' : undefined}><input type="password" autocomplete="new-password" bind:value={password} /></Field>
			<Field variant="chip" layout="stacked" label="Confirm password" error={confirmationError}><input type="password" autocomplete="new-password" bind:value={confirmation} onblur={validateConfirmation} /></Field>
			<Field variant="chip" layout="stacked" label="Email (optional)"><input type="email" autocomplete="email" bind:value={email} /></Field>
			<div class="folder"><span>Work folder</span><strong>{folder}</strong></div>
			{#if accountError}<Message variant="err">{accountError}</Message>{/if}
		</form>
	</SetupShell>
{:else if page === 'error'}
	<SetupShell {steps} {jobs} title="CORSIA couldn’t start" lede={statusError || accountError || 'Try again.'} primary={{ label: 'Try again' }} onPrimary={page === 'error' && !get(token) ? readStatus : createAccount}>
		{#snippet secondary()}<Button variant="chip" onclick={() => page = 'account'}>Back</Button>{/snippet}
	</SetupShell>
{:else}
	<SetupShell {steps} {jobs} title="Choose an AI model" lede="Your agents need at least one. You can add more later in Settings." primary={{ label: 'Start using CORSIA', disabled: !canFinish, note: 'Ready when the download finishes. You can close this window; CORSIA keeps going.' }} onPrimary={onFinished}>
		<div class="sources">
			{#each sources as source (source.source)}
				<ListRow name={source.source === 'omlx' ? 'This computer' : source.name} meta={[source.setup_downloading && source.source === 'omlx' ? 'Downloading in the background' : source.reason || (source.source === 'omlx' ? `Local model · ${modelSize}` : 'Use your subscription')]} centered>
					{#snippet actions()}
						{#if sourceIsReady(source)}<StatePill look="dot" state="success" label="Ready" />
						{:else if source.source === 'omlx' && source.setup_downloading}<StatePill look="dot" state="loading" label="Downloading" />
						{:else if source.source === 'omlx'}<Button variant="chip" size="sm" onclick={async () => { await startLocalDownload(); await refreshSources(); }}>Download</Button>
						{:else}<Button variant="chip" size="sm" onclick={() => signIn(source)}>{source.state === 'not_installed' ? 'Install' : 'Sign in'}</Button>{/if}
					{/snippet}
				</ListRow>
			{/each}
			{#if keyOpen}
				<Field variant="chip" layout="stacked" label="API key">
					<select aria-label="Provider" bind:value={providerName}>{#each $providers.filter((provider) => provider.type === 'cloud') as provider}<option value={provider.name}>{provider.label}</option>{/each}</select>
					<input type="password" aria-label="API key" placeholder="Paste your key" bind:value={apiKey} />
					<Button variant="chip" size="sm" onclick={saveApiKey} disabled={!apiKey.trim() || savingKey}>{savingKey ? 'Saving…' : 'Save'}</Button>
				</Field>
			{:else}<Button variant="chip" size="sm" onclick={() => keyOpen = true}>Use an API key instead</Button>{/if}
			{#if sourceError}<Message variant="err">{sourceError}</Message>{/if}
		</div>
	</SetupShell>
{/if}

<style>
	.form { display: flex; flex-direction: column; gap: var(--space-2); max-width: calc(var(--space-8) * 8); }
	.folder { display: flex; align-items: center; gap: var(--space-3); min-height: var(--space-7); color: var(--text-secondary); font-size: var(--text-subhead); }
	.folder strong { color: var(--text-primary); font-weight: var(--fw-regular); overflow-wrap: anywhere; }
	.welcome-copy, .sources { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
	.welcome-copy p { margin: 0; }
	.download-choice { display: flex; flex-direction: column; gap: var(--space-1); }
	.switch { display: flex; gap: var(--space-2); align-items: center; color: var(--text-primary); }
	.local-meta { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.sources { align-items: flex-start; }
</style>
