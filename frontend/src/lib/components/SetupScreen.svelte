<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { Button, Field, ListRow, Message, Spinner, StatePill, Toggle } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from './SetupShell.svelte';
	import { token, user } from '$lib/stores/auth';
	import { fetchSources, isHarness, recheckSources, setupLocalSource, sourceIsReady, type ModelSource } from '$lib/conductorSources';
	import { accountToSignIn, cancelSignin, checkSignin, getCliInstall, linkCli, startCliInstall, startSignin } from '$lib/conductorSourcesApi';
	import { openExternalUrl } from '$lib/linkTargetResolver';
	import { updateProvider, loadProviders, providers } from '$lib/stores/providers';
	import { on } from '$lib/ws/events';
	import { connect } from '$lib/ws';

	interface Props { onExistingAccount: () => void; onAccountCreated: () => void; onFinished: () => void; }
	const { onExistingAccount, onAccountCreated, onFinished }: Props = $props();
	type Page = 'checking' | 'welcome' | 'account' | 'models' | 'hard-stop' | 'error';
	type SetupStatus = { has_users?: boolean; apple_silicon?: boolean; macos_supported?: boolean; ram_tier?: 'eligible' | 'low' | 'below_minimum'; free_disk_gb?: number; local_eligible?: boolean; app_location?: 'applications' | 'disk_image' | 'other'; work_folder?: string; model_name?: string; model_size_gb?: number; required_username?: string | null; existing_omlx?: boolean; ready_ai?: string | null; error?: string };
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
	let localStartError = $state('');
	let localDownloadPending = $state(false);
	let localSetupBusy = $state(false);
	let existingOmlxConsent = $state(false);
	let keyOpen = $state(false);
	let providerName = $state('anthropic');
	let apiKey = $state('');
	let savingKey = $state(false);
	// One vendor's place in install and sign-in; absent means the row shows its source's own state.
	type ConnectPhase = { phase: 'confirm' | 'installing' | 'signing-in' | 'failed'; installed?: boolean; accountId?: string; failed?: 'install' | 'signin'; reason?: string | null };
	let flow = $state<Record<string, ConnectPhase | undefined>>({});

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
				return { title: 'Welcome to CORSIA', lede: 'CORSIA runs AI agents that work on your projects with you.', primary: { label: 'Get started' }, onPrimary: beginSetup, mark: true };
			case 'account':
				return { title: 'Create your account', primary: { label: status.ready_ai ? 'Start using CORSIA' : 'Continue', disabled: accountBusy || !name.trim() || !username.trim() || !!usernameError || password.length < 8 || !!confirmationError, loading: accountBusy }, onPrimary: createAccount };
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
			// A session opened here has no socket until something connects it; the app shell only connects on mount.
			connect();
			await refreshSources();
			password = ''; confirmation = '';
			// A source that is already Ready means the AI step never appears: the person goes straight in.
			if (canFinish) { onFinished(); return; }
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

	const VENDOR_COPY: Record<string, { cli: string; uses: string; maker: string; origin: string; account: string; page: string }> = {
		'claude-code': { cli: 'Claude Code', uses: 'Uses your Claude subscription.', maker: 'Anthropic', origin: 'claude.ai', account: 'Claude', page: 'https://code.claude.com/docs/en/setup' },
		codex: { cli: 'Codex', uses: 'Uses your ChatGPT subscription.', maker: 'OpenAI', origin: 'chatgpt.com', account: 'ChatGPT', page: 'https://developers.openai.com/codex/cli' },
	};
	const POLL_MS = 3000;
	const connectActive = $derived(Object.values(flow).some((entry) => entry?.phase === 'installing' || entry?.phase === 'signing-in'));

	function setConnect(vendor: string, next: ConnectPhase | undefined) {
		flow = { ...flow, [vendor]: next };
	}

	async function conductorThread(current: string): Promise<string> {
		const conductor = await fetch('/api/conductor', { headers: { Authorization: `Bearer ${current}` } });
		const target = (await conductor.json())?.target?.thread_id;
		if (!target) throw new Error('conductor unavailable');
		return target;
	}

	/** Start the vendor's browser sign-in in place; the poll below sees it complete. */
	async function beginSignIn(source: ModelSource, installed = false) {
		const current = get(token);
		if (!current) return;
		const vendor = source.source;
		setConnect(vendor, { phase: 'signing-in', installed });
		try {
			const target = await conductorThread(current);
			const accountId = await accountToSignIn(current, vendor, source.name);
			const result = await startSignin(current, accountId, target);
			if (result.status === 'failed' || result.status === 'unavailable' || result.status === 'refused') throw new Error(`signin ${result.status}`);
			setConnect(vendor, { phase: 'signing-in', installed, accountId });
			await tickConnect();
		} catch {
			setConnect(vendor, { phase: 'failed', failed: 'signin' });
		}
	}

	async function openSignInAgain(source: ModelSource) {
		const current = get(token);
		const accountId = flow[source.source]?.accountId;
		if (current && accountId) await cancelSignin(current, accountId).catch(() => undefined);
		await beginSignIn(source, flow[source.source]?.installed);
	}

	/** The CLI is now there: sign in if it still needs to, and say it plainly when it is not found. */
	async function afterInstalled(vendor: string) {
		await refreshSources();
		const source = sources.find((candidate) => candidate.source === vendor);
		if (!source || sourceIsReady(source)) { setConnect(vendor, undefined); return; }
		if (source.state === 'not_installed') { setConnect(vendor, { phase: 'failed', failed: 'install', reason: null }); return; }
		await beginSignIn(source, true);
	}

	async function startInstall(source: ModelSource) {
		const current = get(token);
		if (!current) return;
		const vendor = source.source;
		setConnect(vendor, { phase: 'installing' });
		try {
			const refusal = await startCliInstall(current, vendor);
			if (refusal === 'invisible_binary') await linkCli(current, vendor);
			if (refusal) { await afterInstalled(vendor); return; }
			await tickConnect();
		} catch {
			setConnect(vendor, { phase: 'failed', failed: 'install', reason: null });
		}
	}

	let ticking = false;
	/** One look at every install and sign-in under way. */
	async function tickConnect() {
		const current = get(token);
		if (!current || ticking) return;
		ticking = true;
		try {
			for (const [vendor, entry] of Object.entries(flow)) {
				if (entry?.phase === 'installing') {
					const install = await getCliInstall(current, vendor).catch(() => null);
					if (install?.state === 'done' || install?.state === 'idle') await afterInstalled(vendor);
					else if (install?.state === 'failed') setConnect(vendor, { phase: 'failed', failed: 'install', reason: install.reason });
				} else if (entry?.phase === 'signing-in' && entry.accountId) {
					const signin = await checkSignin(current, entry.accountId).catch(() => null);
					if (signin?.status === 'completed') { await refreshSources(); setConnect(vendor, undefined); }
					else if (signin && signin.status !== 'waiting') setConnect(vendor, { phase: 'failed', failed: 'signin' });
				}
			}
		} finally { ticking = false; }
	}

	let rechecking = false;
	/** Coming back to the window is how a copy fixed in Terminal, or a sign-in finished in the browser, is noticed. */
	async function recheckOnFocus() {
		if (page !== 'models' || rechecking) return;
		rechecking = true;
		try {
			await refreshSources();
			await tickConnect();
		} finally { rechecking = false; }
	}

	$effect(() => {
		if (!connectActive) return;
		const timer = setInterval(() => void tickConnect(), POLL_MS);
		return () => clearInterval(timer);
	});

	type RowView = {
		meta: string; ready?: boolean; busy?: boolean; error?: boolean; detail?: string;
		action?: { label: string; run: () => void };
		confirm?: { label: string; run: () => void }[];
		more?: { label: string; run: () => void }[];
	};

	function connectRow(source: ModelSource): RowView {
		const copy = VENDOR_COPY[source.source] ?? { cli: source.name, uses: '', maker: source.name, origin: '', account: source.name, page: '' };
		if (sourceIsReady(source)) return { meta: copy.uses, ready: true };
		const entry = flow[source.source];
		const lines = (rest: string) => `${copy.uses} ${rest}`.trim();
		if (entry?.phase === 'confirm') {
			return {
				meta: lines(`CORSIA installs ${copy.cli} for you.`),
				detail: `Set up ${copy.cli}? This runs ${copy.maker}’s official installer, downloaded from ${copy.origin}, and then you sign in to your ${copy.account} account.`,
				confirm: [{ label: 'Set up', run: () => void startInstall(source) }, { label: 'Cancel', run: () => setConnect(source.source, undefined) }],
			};
		}
		if (entry?.phase === 'installing') return { meta: `Installing with ${copy.maker}’s official installer. This can take up to 3 minutes.`, busy: true };
		if (entry?.phase === 'signing-in') {
			return {
				meta: `${entry.installed ? 'Installed. ' : ''}Finish signing in to your ${copy.account} account in the browser window that just opened.`,
				busy: true, action: { label: 'Open sign-in again', run: () => void openSignInAgain(source) },
			};
		}
		if (entry?.phase === 'failed' && entry.failed === 'signin') {
			return { meta: lines(`${copy.cli} is installed.`), error: true, detail: `Couldn’t sign in to ${copy.cli}. Choose Sign in to try again.`, action: { label: 'Sign in', run: () => void beginSignIn(source) } };
		}
		if (entry?.phase === 'failed') {
			const why = entry.reason ? `: ${entry.reason}` : '';
			const next = entry.reason?.includes('internet') ? 'Check your connection, then choose Try again.' : 'Choose Try again.';
			return {
				meta: lines(''), error: true, detail: `Couldn’t install ${copy.cli}${why}. ${next}`,
				action: { label: 'Try again', run: () => void startInstall(source) },
				more: copy.page ? [{ label: 'Open the install page', run: () => void openExternalUrl(copy.page) }] : undefined,
			};
		}
		if (source.state === 'not_installed') return { meta: lines(`CORSIA installs ${copy.cli} for you.`), action: { label: 'Set up', run: () => setConnect(source.source, { phase: 'confirm' }) } };
		if (source.limited) return { meta: lines('It has reached its usage limit for now.') };
		if (source.state === 'unverified') return { meta: lines(`CORSIA couldn’t check this copy of ${copy.cli}.`), action: { label: 'Try again', run: () => void refreshSources() } };
		return { meta: lines(`${copy.cli} is installed. Sign in to use it.`), action: { label: 'Sign in', run: () => void beginSignIn(source) } };
	}

	const harnessSources = $derived(sources.filter(isHarness));
	const showLocalRow = $derived(Boolean(local) && (localDownloading || Boolean(local?.setup_error || localStartError) || (local && sourceIsReady(local))));

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
		const onVisible = () => { if (document.visibilityState === 'visible') void recheckOnFocus(); };
		const onFocus = () => void recheckOnFocus();
		window.addEventListener('focus', onFocus);
		document.addEventListener('visibilitychange', onVisible);
		return () => { offModels(); window.removeEventListener('focus', onFocus); document.removeEventListener('visibilitychange', onVisible); };
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
			<div class="rows">
				{#each harnessSources as source (source.source)}
					{@const row = connectRow(source)}
					<section class="item" aria-label={VENDOR_COPY[source.source]?.cli ?? source.name}>
						<ListRow name={VENDOR_COPY[source.source]?.cli ?? source.name} meta={[row.meta]} centered>
							{#snippet actions()}
								{#if row.ready}<StatePill look="dot" state="success" label="Ready" />
								{:else if row.busy && !row.action}<Spinner size={14} />
								{:else if row.action}<Button variant="chip" size="sm" onclick={row.action.run}>{row.action.label}</Button>{/if}
							{/snippet}
						</ListRow>
						{#if row.detail}<p class="detail" class:err={row.error}>{row.detail}</p>{/if}
						{#if row.confirm}
							<div class="inrow">{#each row.confirm as item (item.label)}<Button variant="chip" size="sm" onclick={item.run}>{item.label}</Button>{/each}</div>
						{:else if row.more}
							<div class="inrow">{#each row.more as item (item.label)}<Button variant="chip" size="sm" onclick={item.run}>{item.label}</Button>{/each}</div>
						{/if}
					</section>
				{/each}
				{#if showLocalRow && local}
					<section class="item" aria-label="This computer">
						<ListRow name="This computer" meta={[sourceMeta(local)]} centered>
							{#snippet actions()}
								{#if sourceIsReady(local)}<StatePill look="dot" state="success" label="Ready" />
								{:else if localDownloading}<StatePill look="dot" state="loading" label="Downloading" />
								{:else}<Button variant="chip" size="sm" loading={localSetupBusy} disabled={localSetupBusy} onclick={retryLocalDownload}>Try again</Button>{/if}
							{/snippet}
						</ListRow>
						{#if !localDownloading && !sourceIsReady(local)}<p class="detail err">{downloadModelLabel} didn’t finish downloading. Choose Try again, or use another source.</p>{/if}
					</section>
				{/if}
			</div>
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
	.folder { display: flex; align-items: center; gap: var(--space-3); min-height: var(--space-7); color: var(--text-secondary); font-size: var(--text-subhead); }
	.folder span { flex: none; white-space: nowrap; }
	.folder strong { color: var(--text-primary); font-weight: var(--fw-regular); overflow-wrap: anywhere; }
	.welcome-copy, .sources { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
	.welcome-copy p, .consent p { margin: 0; }
	.consent ul { margin: 0; padding-left: var(--space-5); }
	.consent-actions { display: flex; gap: var(--space-2); }
	.download-choice { display: flex; flex-direction: column; gap: var(--space-1); }
	.switch { display: flex; gap: var(--space-2); align-items: center; color: var(--text-primary); }
	.local-meta { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.api-key-action { align-self: flex-start; }
	/* Rows keep the narrow window's width in a wide one, so an action stays beside its text. */
	.rows { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 10); }
	.detail { margin: var(--space-2) 0 0; font-size: var(--text-subhead); color: var(--text-secondary); line-height: var(--lh-normal); text-wrap: pretty; }
	.detail.err { color: var(--red-text); }
	.inrow { display: flex; gap: var(--space-2); margin: var(--space-1) 0 0 calc(var(--space-2) * -1); }
</style>
