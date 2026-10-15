<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { Button, Field, ListRow, Message, StatePill, Toggle } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from './SetupShell.svelte';
	import SetupOmlxConsent from './SetupOmlxConsent.svelte';
	import { token, user } from '$lib/stores/auth';
	import { fetchSources, recheckSources, setupLocalSource, sourceIsReady, type ModelSource } from '$lib/conductorSources';
	import { accountToSignIn, startSignin } from '$lib/conductorSourcesApi';
	import { updateProvider, loadProviders, providers } from '$lib/stores/providers';
	import { on } from '$lib/ws/events';
	import { connect } from '$lib/ws';

	interface Props { onExistingAccount: () => void; onAccountCreated: () => void; onFinished: () => void; }
	const { onExistingAccount, onAccountCreated, onFinished }: Props = $props();
	type Page = 'checking' | 'welcome' | 'account' | 'models' | 'hard-stop' | 'error';
	type SetupStatus = { has_users?: boolean; apple_silicon?: boolean; macos_supported?: boolean; ram_tier?: 'eligible' | 'low' | 'below_minimum'; free_disk_gb?: number; local_eligible?: boolean; app_location?: 'applications' | 'disk_image' | 'other'; work_folder?: string; model_name?: string; model_size_gb?: number; required_username?: string | null; existing_omlx?: boolean; error?: string };
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
	let accountComplete = $state(Boolean(get(token)));
	let accountError = $state('');
	let confirmationError = $state('');
	let sources = $state<ModelSource[]>([]);
	let sourceError = $state('');
	let localStartError = $state('');
	let localDownloadPending = $state(false);
	let localPendingStage = $state<string | null>(null);
	let localSetupBusy = $state(false);
	let existingOmlxConsent = $state(false);
	let replaceOmlxConsent = $state(false);
	let localRepairDeferred = $state(false);
	let keyOpen = $state(false);
	let providerName = $state('anthropic');
	let apiKey = $state('');
	let savingKey = $state(false);

	const steps = $derived<SetupStep[]>([
		{ label: 'Welcome', icon: 'sparkle', state: page === 'welcome' || page === 'hard-stop' || page === 'checking' ? 'current' : 'done' },
		{ label: 'Your account', icon: 'person', state: page === 'account' ? 'current' : accountComplete ? 'done' : 'todo' },
		{ label: 'AI model', icon: 'chip', state: page === 'models' ? 'current' : 'todo' },
	]);
	const local = $derived(sources.find((source) => source.source === 'omlx'));
	const localProgress = $derived(local?.download_percent == null ? undefined : local.download_percent / 100);
	const localStage = $derived(local?.setup_stage || localPendingStage || 'Downloading');
	const localDownloading = $derived(Boolean(local?.setup_downloading || local?.setup_stage || (localDownloadPending && !local?.setup_error)));
	const localJob = $derived<SetupJob | undefined>(
		localDownloading
			? { label: status.model_name || local?.name || 'Local model', state: 'busy', meta: localStage, progress: localProgress }
			: local?.setup_error || localStartError
				? { label: status.model_name || local?.name || 'Local model', state: 'error', meta: 'Couldn’t download' }
				: undefined,
	);
	const canFinish = $derived(sources.some(sourceIsReady));
	const jobs = $derived<SetupJob[]>([
		...(localJob ? [localJob] : []),
		...(page === 'account' && accountBusy ? [{ label: 'Starting CORSIA', state: 'busy' as const, meta: 'Preparing your account' }] : []),
		...(page === 'error' ? [{ label: 'Starting CORSIA', state: 'error' as const, meta: 'Couldn’t start' }] : []),
		...(page === 'models' && canFinish && !localDownloading ? [{ label: 'Starting CORSIA', state: 'done' as const, meta: 'Ready' }] : []),
	]);
	const finishNote = $derived(
		localStartError || local?.setup_error
			? 'The local model couldn’t start. Try again, or choose another source.'
			: localDownloading
				? 'Ready when the download finishes. You can close this window; CORSIA keeps going.'
				: 'Sign in, add an API key, or download the local model to continue.',
	);
	const modelSize = $derived(status.model_size_gb ? `${status.model_size_gb} GB` : 'a local model');
	const downloadModelLabel = $derived(status.model_name || 'local model');
	const usernameError = $derived(username && !/^[a-z0-9_-]{1,32}$/.test(username) ? "Use 1–32 lowercase letters, numbers, '-' or '_'." : '');
	type SetupPrimary = { label: string; disabled?: boolean; loading?: boolean; note?: string };
	type SetupView = { title: string; lede?: string; primary?: SetupPrimary; onPrimary?: () => void; mark?: boolean };
	const shellView = $derived.by((): SetupView => {
		switch (page) {
			case 'checking':
				return { title: 'Checking this computer', lede: 'This takes a moment.' };
			case 'hard-stop':
				if (status.apple_silicon === false) return { title: 'CORSIA needs Apple silicon', lede: 'This version of CORSIA runs on Macs with Apple silicon.' };
				if (status.macos_supported === false) return { title: 'Update macOS to continue', lede: 'Update to macOS 15 or later, then open CORSIA again.' };
				return { title: 'Move CORSIA to Applications', lede: 'Move CORSIA to Applications, then open it again so updates work.' };
			case 'welcome':
				return { title: 'Welcome to CORSIA', lede: 'CORSIA runs AI agents that work on your projects with you.', primary: { label: 'Get started' }, onPrimary: beginSetup, mark: true };
			case 'account':
				return { title: 'Create your account', primary: { label: 'Continue', disabled: accountBusy || !name.trim() || !username.trim() || !!usernameError || password.length < 8 || !!confirmationError, loading: accountBusy }, onPrimary: createAccount };
			case 'error':
				return { title: 'CORSIA couldn’t start', lede: statusError || accountError || 'Try again.', primary: { label: 'Try again' }, onPrimary: !get(token) ? readStatus : createAccount };
			case 'models':
				if (replaceOmlxConsent || existingOmlxConsent) {
					const replacing = replaceOmlxConsent;
					return {
						title: replacing ? "The oMLX on this computer doesn't start" : 'Use your oMLX?',
						lede: replacing ? undefined : 'Your oMLX is already on this computer.',
						primary: { label: replacing ? 'Reinstall' : 'Use it', disabled: localSetupBusy, loading: localSetupBusy },
						onPrimary: () => void decideExistingOmlx(true),
					};
				}
				return { title: 'Choose an AI model', lede: 'Your agents need at least one. You can add more later in Settings.', primary: { label: 'Start using CORSIA', disabled: !canFinish, note: finishNote }, onPrimary: onFinished };
		}
	});

	async function readStatus() {
		statusError = '';
		page = 'checking';
		try {
			const response = await fetch('/api/setup/status');
			if (response.status === 409) { onExistingAccount(); return; }
			if (!response.ok) throw new Error(`setup status ${response.status}`);
			status = await response.json();
			folder = status.work_folder || folder;
			if (status.required_username) username = status.required_username;
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

	type LocalDownloadStart = 'started' | 'consent_required' | 'replace_consent_required' | 'failed';

	async function startLocalDownload(consentExisting = false, consentReplace = false): Promise<LocalDownloadStart> {
		localPendingStage = null;
		if (!localWanted && !consentExisting && !consentReplace) return 'started';
		const response = await fetch('/api/setup/local-download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ consent_existing: consentExisting, ...(consentReplace ? { consent_replace: true } : {}) }) });
		const result = await response.json().catch(() => null);
		if (response.status === 409 && (result?.code === 'consent_required' || result?.code === 'replace_consent_required')) return result.code;
		if (response.ok && result?.ok !== false) {
			localPendingStage = typeof result?.stage === 'string' ? result.stage : null;
			return 'started';
		}
		return 'failed';
	}

	async function beginSetup() {
		if (status.existing_omlx) {
			existingOmlxConsent = true;
			page = 'models';
			return;
		}
		try {
			const start = await startLocalDownload();
			if (start === 'consent_required') {
				existingOmlxConsent = true;
				page = 'models';
				return;
			}
			if (start === 'replace_consent_required') {
				replaceOmlxConsent = true;
				page = 'models';
				return;
			}
			if (start === 'started') { localRepairDeferred = false; localDownloadPending = localWanted; }
			else localStartError = 'The local model download couldn’t start.';
		} catch {
			localStartError = 'The local model download couldn’t start.';
		}
		page = 'account';
	}

	async function decideExistingOmlx(useIt: boolean) {
		if (!useIt) {
			const current = get(token);
			existingOmlxConsent = false;
			localRepairDeferred = replaceOmlxConsent;
			replaceOmlxConsent = false;
			localWanted = false;
			page = current ? 'models' : 'account';
			if (current) void refreshSources();
			return;
		}
		const replacing = replaceOmlxConsent;
		localSetupBusy = true;
		if (replacing) localDownloadPending = true;
		localStartError = '';
		let returnToModels = false;
		let needsReplacementConsent = false;
		let localStartSucceeded = false;
		try {
			const current = get(token);
			returnToModels = Boolean(current);
			const start = current
				? await setupLocalSource(current, !replacing, replacing)
				: await startLocalDownload(!replacing, replacing);
			const code = typeof start === 'string' ? start : start.code;
			if (code === 'replace_consent_required') needsReplacementConsent = true;
			else if ((typeof start === 'string' ? start === 'started' : start.ok)) { localRepairDeferred = false; localDownloadPending = true; localStartSucceeded = true; }
			else localStartError = 'The local model couldn’t start.';
		} catch {
			localStartError = 'The local model couldn’t start.';
		} finally {
			localSetupBusy = false;
			if (!localStartSucceeded) localDownloadPending = false;
			existingOmlxConsent = false;
			replaceOmlxConsent = needsReplacementConsent;
			page = needsReplacementConsent || returnToModels ? 'models' : 'account';
		}
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
			token.set(result.token); user.set(result.user ?? { id: result.user_id, name: name.trim() }); accountComplete = true;
			// A session opened here has no socket until something connects it; the app shell only connects on mount.
			connect();
			await refreshSources();
			password = ''; confirmation = '';
			page = 'models';
		} catch { accountError = 'CORSIA couldn’t create your account. Try again.'; }
		finally { accountBusy = false; }
	}

	async function refreshSources() {
		const current = get(token);
		if (!current) return;
		try {
			sources = await recheckSources(current);
			localDownloadPending = false;
			localPendingStage = null;
			sourceError = '';
		} catch {
			try {
				sources = await fetchSources(current);
				localDownloadPending = false;
				localPendingStage = null;
				sourceError = '';
			} catch { sourceError = 'CORSIA couldn’t check your model sources. Try again.'; }
		}
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

	async function retryLocalDownload() {
		const current = get(token);
		if (!current || localSetupBusy || localDownloading) return;
		if (status.existing_omlx) {
			existingOmlxConsent = true;
			return;
		}
		localSetupBusy = true;
		sourceError = '';
		localStartError = '';
		try {
			const result = await setupLocalSource(current);
			if (result.code === 'consent_required') {
				existingOmlxConsent = true;
				return;
			}
			if (result.code === 'replace_consent_required') {
				replaceOmlxConsent = true;
				return;
			}
			if (!result.ok) sourceError = 'The local model couldn’t start. Try again, or choose another source.';
		} catch {
			sourceError = 'The local model couldn’t start. Try again, or choose another source.';
		} finally {
			await refreshSources();
			localSetupBusy = false;
		}
	}

	function sourceMeta(source: ModelSource): string {
		if (source.source === 'omlx') {
			if (source.setup_downloading || source.setup_stage) return source.setup_stage || 'Downloading';
			if (localRepairDeferred) return "oMLX doesn't start";
			if (source.setup_error || localStartError) return 'Download needs attention';
			return `Local model · ${modelSize}`;
		}
		if (source.state === 'not_installed') return `${source.name} needs its official app installed first.`;
		if (source.state === 'needs_auth') return 'Sign in to use your subscription';
		if (sourceIsReady(source)) return 'Ready to use.';
		return 'Check this source, then try again.';
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

	onMount(() => {
		void readStatus();
		return on('models_updated', () => { if (page === 'models') void refreshSources(); });
	});
</script>

<SetupShell {steps} {jobs} page={page} title={shellView.title} lede={shellView.lede} primary={shellView.primary} mark={shellView.mark} onPrimary={shellView.onPrimary}>
	{#snippet secondary()}
		{#if page === 'hard-stop'}
			<Button variant="chip" onclick={readStatus}>Check again</Button>
		{:else if page === 'error'}
			<Button variant="chip" onclick={() => page = 'account'}>Back</Button>
		{/if}
	{/snippet}

	{#if page === 'welcome'}
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
	{:else if page === 'account'}
		<form class="form" onsubmit={(event) => { event.preventDefault(); createAccount(); }}>
			<Field variant="chip" layout="stacked" label="Name" labelFor="setup-name"><input id="setup-name" autocomplete="name" bind:value={name} /></Field>
			<Field variant="chip" layout="stacked" label="Username" labelFor="setup-username" error={usernameError || undefined} hint={status.required_username ? 'This matches the existing data on this computer.' : undefined}><input id="setup-username" autocomplete="username" autocapitalize="off" bind:value={username} readonly={!!status.required_username} /></Field>
			<Field variant="chip" layout="stacked" label="Password" labelFor="setup-password" error={password && password.length < 8 ? 'Use at least 8 characters.' : undefined}><input id="setup-password" type="password" autocomplete="new-password" bind:value={password} /></Field>
			<Field variant="chip" layout="stacked" label="Confirm password" labelFor="setup-confirm-password" error={confirmationError}><input id="setup-confirm-password" type="password" autocomplete="new-password" bind:value={confirmation} onblur={validateConfirmation} /></Field>
			<Field variant="chip" layout="stacked" label="Email (optional)" labelFor="setup-email"><input id="setup-email" type="email" autocomplete="email" bind:value={email} /></Field>
			<div class="folder"><span>Work folder</span><strong>{folder}</strong></div>
			{#if accountError}<Message variant="err">{accountError}</Message>{/if}
		</form>
	{:else if page === 'models' && (existingOmlxConsent || replaceOmlxConsent)}
		<SetupOmlxConsent replace={replaceOmlxConsent} modelLabel={downloadModelLabel} modelSize={modelSize} busy={localSetupBusy} onDecision={decideExistingOmlx} />
	{:else if page === 'models'}
		<div class="sources">
			{#each sources as source (source.source)}
				<ListRow name={source.source === 'omlx' ? 'This computer' : source.name} meta={[sourceMeta(source)]} centered>
					{#snippet actions()}
						{#if sourceIsReady(source)}<StatePill look="dot" state="success" label="Ready" />
						{:else if source.source === 'omlx' && localDownloading}<StatePill look="dot" state="loading" label="Downloading" />
						{:else if source.source === 'omlx'}<Button variant="chip" size="sm" loading={localSetupBusy} disabled={localSetupBusy} onclick={retryLocalDownload}>{localRepairDeferred ? 'Reinstall' : source.setup_error || localStartError ? 'Try again' : 'Download'}</Button>
						{:else if source.state === 'not_installed'}<StatePill look="dot" state="idle" label="Not installed" /><Button variant="chip" size="sm" onclick={refreshSources}>Check again</Button>
						{:else}<Button variant="chip" size="sm" onclick={() => signIn(source)}>Sign in</Button>{/if}
					{/snippet}
				</ListRow>
			{/each}
			{#if keyOpen}
				<Field variant="chip" layout="stacked" label="API key">
					<select aria-label="Provider" bind:value={providerName}>{#each $providers.filter((provider) => provider.type === 'cloud') as provider}<option value={provider.name}>{provider.label}</option>{/each}</select>
					<input type="password" aria-label="API key" placeholder="Paste your key" bind:value={apiKey} />
					<Button variant="chip" size="sm" onclick={saveApiKey} disabled={!apiKey.trim() || savingKey}>{savingKey ? 'Saving…' : 'Save'}</Button>
				</Field>
			{:else}<div class="api-key-action"><Button variant="chip" size="sm" onclick={() => keyOpen = true}>Use an API key instead</Button></div>{/if}
			{#if sourceError}<Message variant="err">{sourceError}</Message>{/if}
		</div>
	{/if}
</SetupShell>
<style>
	.form { display: flex; flex-direction: column; gap: var(--space-2); max-width: calc(var(--space-8) * 8); }
	.folder { display: flex; align-items: center; gap: var(--space-3); min-height: var(--space-7); color: var(--text-secondary); font-size: var(--text-subhead); } .folder span { flex: none; white-space: nowrap; } .folder strong { color: var(--text-primary); font-weight: var(--fw-regular); overflow-wrap: anywhere; }
	.welcome-copy, .sources { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); } .welcome-copy p { margin: 0; }
	.download-choice { display: flex; flex-direction: column; gap: var(--space-1); } .switch { display: flex; gap: var(--space-2); align-items: center; color: var(--text-primary); } .local-meta { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.api-key-action { align-self: flex-start; }
</style>
