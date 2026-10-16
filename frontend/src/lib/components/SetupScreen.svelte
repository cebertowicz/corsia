<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { Button, Field, Message } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from './SetupShell.svelte';
	import SetupAiRows, { type AiRow } from './SetupAiRows.svelte';
	import SetupWelcomeChoice from './SetupWelcomeChoice.svelte';
	import { token, user } from '$lib/stores/auth';
	import { fetchSources, isHarness, recheckSources, setupLocalSource, sourceIsReady, type ModelSource } from '$lib/conductorSources';
	import { createSetupConnect, POLL_MS, VENDOR_COPY } from '$lib/setupConnect.svelte';
	import { updateProvider, loadProviders, providers } from '$lib/stores/providers';
	import { on } from '$lib/ws/events';
	import { connect } from '$lib/ws';
	import { createSetupClassifier, type ClassifierState } from '$lib/setupClassifier.svelte';
	import SetupAccountForm from './SetupAccountForm.svelte';
	interface Props { onExistingAccount: () => void; onAccountCreated: () => void; onFinished: () => void; }
	const { onExistingAccount, onAccountCreated, onFinished }: Props = $props();
	type Page = 'checking' | 'welcome' | 'account' | 'models' | 'hard-stop' | 'error';
	type SetupStatus = { has_users?: boolean; apple_silicon?: boolean; macos_supported?: boolean; ram_tier?: 'eligible' | 'low' | 'below_minimum'; free_disk_gb?: number; local_eligible?: boolean; app_location?: 'applications' | 'disk_image' | 'other'; work_folder?: string; model_name?: string; model_size_gb?: number; required_username?: string | null; existing_omlx?: boolean; classifier?: ClassifierState; ready_ai?: string | null; error?: string };
	type FolderNotice = { kind: 'protected'; folder: string } | { kind: 'unwritable'; reason: string } | null;
	const classifier = createSetupClassifier();
	const connectFlow = createSetupConnect({ token: () => get(token), sources: () => sources, refreshSources: () => refreshSources() });
	let page = $state<Page>('checking');
	let status = $state<SetupStatus>({});
	let localWanted = $state(true);
	let statusError = $state('');
	let name = $state('');
	let username = $state('');
	let password = $state('');
	let confirmation = $state('');
	let email = $state('');
	let folder = $state('~/CORSIA'), folderChanged = $state(false);
	let folderNotice = $state<FolderNotice>(null);
	let folderChanging = $state(false);
	let accountBusy = $state(false);
	let accountError = $state('');
	let confirmationError = $state('');
	let sources = $state<ModelSource[]>([]);
	let sourceError = $state('');
	let localStartError = $state('');
	let localDownloadPending = $state(false);
	let localSetupBusy = $state(false);
	let existingOmlxConsent = $state(false);
	let keyOpen = $state(false);
	let providerName = $state('anthropic');
	let apiKey = $state('');
	let savingKey = $state(false);

	const steps = $derived<SetupStep[]>([
		{ label: 'Welcome', icon: 'sparkle', state: page === 'welcome' || page === 'hard-stop' || page === 'checking' ? 'current' : 'done' },
		{ label: 'Your account', icon: 'person', state: page === 'account' ? 'current' : page === 'models' && !existingOmlxConsent ? 'done' : 'todo' },
		{ label: status.ready_ai && page !== 'models' ? status.ready_ai : 'Your AI', icon: 'chip', state: page === 'models' ? 'current' : status.ready_ai ? 'done' : 'todo' },
	]);
	const local = $derived(sources.find((source) => source.source === 'omlx'));
	const localProgress = $derived(local?.download_percent == null ? undefined : local.download_percent / 100);
	const localJobMeta = $derived(local?.download_percent == null ? local?.setup_stage || 'Downloading' : `${local.download_percent}%`);
	const localDownloading = $derived(Boolean(local?.setup_downloading || local?.setup_stage || (localDownloadPending && !local?.setup_error)));
	const localJob = $derived<SetupJob | undefined>(
		localDownloading
			? { label: status.model_name || local?.name || 'Local model', state: 'busy', meta: localJobMeta, progress: localProgress }
			: local?.setup_error || localStartError
				? { label: status.model_name || local?.name || 'Local model', state: 'error', meta: 'Couldn’t download' }
				: undefined,
	);
	const canFinish = $derived(sources.some(sourceIsReady));
	const jobs = $derived<SetupJob[]>([
		...(localJob ? [localJob] : []),
		...(classifier.job ? [classifier.job] : []),
		...(page === 'account' && accountBusy ? [{ label: 'Starting CORSIA', state: 'busy' as const, meta: 'Preparing your account' }] : []),
		...(page === 'error' ? [{ label: 'Starting CORSIA', state: 'error' as const, meta: 'Couldn’t start' }] : []),
		...(page === 'models' && canFinish && !localDownloading ? [{ label: 'Starting CORSIA', state: 'done' as const, meta: 'Ready' }] : []),
	]);
	const finishNote = $derived(
		localStartError || local?.setup_error
			? 'The local model couldn’t start. Try again, or choose another source.'
			: localDownloading
				? 'Ready when the download finishes. You can close this window; CORSIA keeps going.'
				: 'Connect Claude Code or Codex, or add an API key, to continue.',
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
				return { title: 'Welcome to CORSIA', lede: 'Your own AI team, on this computer. It remembers your projects, works with the tools and models you already use, and shows you results, not transcripts.', primary: { label: 'Get started' }, onPrimary: beginSetup, mark: true };
			case 'account':
				return { title: 'Create your account', primary: { label: status.ready_ai ? 'Start using CORSIA' : 'Continue', disabled: accountBusy || folderChanging || folderNotice?.kind === 'unwritable' || !name.trim() || !username.trim() || !!usernameError || password.length < 8 || !!confirmationError, loading: accountBusy }, onPrimary: createAccount };
			case 'error':
				return { title: 'CORSIA couldn’t start', lede: statusError || accountError || 'Try again.', primary: { label: 'Try again' }, onPrimary: !get(token) ? readStatus : createAccount };
			case 'models':
				if (existingOmlxConsent) return { title: 'Use your oMLX?', lede: 'Your oMLX is already on this computer.' };
				return { title: 'Connect an AI', lede: 'CORSIA needs one to start. You can add the others with CORSIA later.', primary: { label: 'Start using CORSIA', disabled: !canFinish, note: finishNote }, onPrimary: onFinished };
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
			if (status.classifier) classifier.accept(status.classifier);
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

	type LocalDownloadStart = 'started' | 'consent_required' | 'failed';

	async function startLocalDownload(consentExisting = false): Promise<LocalDownloadStart> {
		if (!localWanted && !consentExisting) return 'started';
		const response = await fetch('/api/setup/local-download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ consent_existing: consentExisting }) });
		const result = await response.json().catch(() => null);
		if (response.status === 409 && result?.code === 'consent_required') return 'consent_required';
		return response.ok && result?.ok !== false ? 'started' : 'failed';
	}

	async function beginSetup() {
		void classifier.start();
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
			if (start === 'started') localDownloadPending = localWanted;
			else localStartError = 'The local model download couldn’t start.';
		} catch {
			localStartError = 'The local model download couldn’t start.';
		}
		page = 'account';
	}

	async function decideExistingOmlx(useIt: boolean) {
		if (!useIt) {
			existingOmlxConsent = false;
			localWanted = false;
			page = 'account';
			return;
		}
		localSetupBusy = true;
		localStartError = '';
		let returnToModels = false;
		try {
			const current = get(token);
			returnToModels = Boolean(current);
			const start = current
				? await setupLocalSource(current, true)
				: await startLocalDownload(true);
			const started = typeof start === 'string' ? start === 'started' : start.ok;
			if (started) localDownloadPending = true;
			else localStartError = 'The local model couldn’t start.';
		} catch {
			localStartError = 'The local model couldn’t start.';
		} finally {
			localSetupBusy = false;
			existingOmlxConsent = false;
			page = returnToModels ? 'models' : 'account';
		}
	}

	async function applyFolderBeforeAccount() {
		if (!folderChanged) return;
		const invoke = (window as Window & { __TAURI_INTERNALS__?: { invoke?: (command: string, args?: Record<string, unknown>) => Promise<unknown> } }).__TAURI_INTERNALS__?.invoke;
		if (!invoke) return;
		await invoke('configure_work_folder', { path: folder });
		folderChanged = false;
	}

	function validateConfirmation() {
		confirmationError = confirmation && password !== confirmation ? 'Passwords don’t match.' : '';
	}

	async function createAccount() {
		validateConfirmation();
		if (!name.trim() || !username.trim() || usernameError || password.length < 8 || confirmationError || accountBusy) return;
		accountBusy = true; accountError = '';
		try {
			await applyFolderBeforeAccount();
		} catch {
			accountError = 'CORSIA couldn’t prepare this work folder. Choose another folder or try again.';
			accountBusy = false;
			return;
		}
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
			// A session opened here has no socket until something connects it; the app shell only connects on mount.
			connect();
			await Promise.all([refreshSources(), classifier.read()]);
			password = ''; confirmation = '';
			// A source already Ready (named by setup status, confirmed by the recheck above) means the AI step never appears.
			if (status.ready_ai && canFinish) { onFinished(); return; }
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
			sourceError = '';
		} catch {
			try {
				sources = await fetchSources(current);
				localDownloadPending = false;
				sourceError = '';
			} catch { sourceError = 'CORSIA couldn’t check your model sources. Try again.'; }
		}
	}

	let rechecking = false;
	/** Coming back to the window is how a copy fixed in Terminal, or a sign-in finished in the browser, is noticed. */
	async function recheckOnFocus() {
		if (page !== 'models' || rechecking) return;
		rechecking = true;
		try {
			await refreshSources();
			await connectFlow.tick();
		} finally { rechecking = false; }
	}

	$effect(() => {
		if (!connectFlow.active) return;
		const timer = setInterval(() => void connectFlow.tick(), POLL_MS);
		return () => clearInterval(timer);
	});

	const harnessSources = $derived(sources.filter(isHarness));
	const showLocalRow = $derived(Boolean(local) && (localDownloading || Boolean(local?.setup_error || localStartError) || (local && sourceIsReady(local))));
	const aiRows = $derived.by((): AiRow[] => {
		const rows: AiRow[] = harnessSources.map((source) => {
			const row = connectFlow.row(source);
			return { key: source.source, name: VENDOR_COPY[source.source]?.cli ?? source.name, meta: row.meta, busy: row.busy, error: row.error, detail: row.detail, action: row.action, confirm: row.confirm, more: row.more,
				pill: row.ready ? { state: 'success', label: 'Ready' } : undefined };
		});
		if (!showLocalRow || !local) return rows;
		const ready = sourceIsReady(local);
		rows.push({ key: 'omlx', name: 'This computer', meta: sourceMeta(local),
			pill: ready ? { state: 'success', label: 'Ready' } : localDownloading ? { state: 'loading', label: 'Downloading' } : undefined,
			action: ready || localDownloading ? undefined : { label: 'Try again', run: retryLocalDownload, loading: localSetupBusy },
			error: !localDownloading && !ready, detail: !localDownloading && !ready ? `${downloadModelLabel} didn’t finish downloading. Choose Try again, or use another source.` : undefined });
		return rows;
	});

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
			if (source.setup_downloading || source.setup_stage) {
				return source.download_percent == null && source.setup_stage ? source.setup_stage : 'Downloading in the background';
			}
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
		const offModels = on('models_updated', () => { if (page === 'models') void refreshSources(); });
		const offClassifier = on('classifier_updated', () => { void classifier.read(); });
		const onVisible = () => { if (document.visibilityState === 'visible') void recheckOnFocus(); };
		const onFocus = () => { void classifier.recheckSpace(); void recheckOnFocus(); };
		window.addEventListener('focus', onFocus);
		document.addEventListener('visibilitychange', onVisible);
		return () => { offModels(); offClassifier(); window.removeEventListener('focus', onFocus); document.removeEventListener('visibilitychange', onVisible); };
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
		<SetupWelcomeChoice localEligible={status.local_eligible} freeDiskGb={status.free_disk_gb} modelSizeGb={status.model_size_gb} {modelSize} modelLabel={downloadModelLabel}
			wanted={localWanted} onWantedChange={(next) => localWanted = next} onRecheck={readStatus} />
	{:else if page === 'account'}
		<SetupAccountForm bind:folder bind:folderChanged bind:name bind:username bind:password bind:confirmation bind:email {usernameError} {confirmationError} requiredUsername={status.required_username} {accountError} {accountBusy} bind:folderChanging bind:folderNotice onSubmit={createAccount} onValidateConfirmation={validateConfirmation} />
	{:else if page === 'models' && existingOmlxConsent}
		<div class="sources consent">
			<p>CORSIA will read:</p>
			<ul>
				<li>your oMLX port setting</li>
				<li>the installed oMLX version</li>
				<li>your oMLX server to find its available models</li>
			</ul>
			<p>It won’t change your oMLX.</p>
			<div class="consent-actions">
				<Button variant="chip" onclick={() => decideExistingOmlx(false)} disabled={localSetupBusy}>Not now</Button>
				<Button variant="chip" onclick={() => decideExistingOmlx(true)} loading={localSetupBusy} disabled={localSetupBusy}>Use it</Button>
			</div>
		</div>
	{:else if page === 'models'}
		<div class="sources">
			<SetupAiRows rows={aiRows} />
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
	/* Account form styles live with SetupAccountForm. */
	.sources { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
	.consent p { margin: 0; }
	.consent ul { margin: 0; padding-left: var(--space-5); }
	.consent-actions { display: flex; gap: var(--space-2); }
	.api-key-action { align-self: flex-start; }
</style>
