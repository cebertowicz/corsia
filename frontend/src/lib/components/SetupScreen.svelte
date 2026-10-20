<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { get } from 'svelte/store';
	import { Button, Message } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from './SetupShell.svelte';
	import SetupAiRows, { type AiRow } from './SetupAiRows.svelte';
	import SetupWelcomeChoice from './SetupWelcomeChoice.svelte';
	import type { SetupPicks } from './SetupSubscriptionQuestion.svelte';
	import SetupApiKeyRow from './SetupApiKeyRow.svelte';
	import SetupExistingOmlxConsent from './SetupExistingOmlxConsent.svelte';
	import { token, user } from '$lib/stores/auth';
	import { fetchSources, isHarness, recheckSources, setupLocalSource, sourceIsReady, type ModelSource } from '$lib/conductorSources';
	import { createSetupConnect, POLL_MS, VENDOR_COPY } from '$lib/setupConnect.svelte';
	import { loadProviders, providers } from '$lib/stores/providers';
	import { on } from '$lib/ws/events';
	import { connect } from '$lib/ws';
	import { finishSetupWithBestEffortSelection } from '$lib/setupFirstModel';
	import { fetchSetupProgress, recordSetupProgress } from '$lib/setupProgress';
	import { saveSetupReport, setupReportInvoke } from '$lib/setupReport';
	import { createSetupClassifier, type ClassifierState } from '$lib/setupClassifier.svelte';
	import { createSetupLocalPick, noLocalModel } from '$lib/setupLocalPick.svelte';
	import { createSetupPicks } from '$lib/setupPicks.svelte';
	import { createSetupLocalActions } from '$lib/setupLocalActions.svelte';
	import SetupAccountForm from './SetupAccountForm.svelte';
	import SetupConnect from './SetupConnect.svelte';
	import SetupConnectBack from './SetupConnectBack.svelte';
	import { createSetupRemote } from '$lib/setupRemote.svelte';
	import { applyWorkFolder, shellInfo } from '$lib/remoteShell';
	import { localJob as buildLocalJob, setupFinishNote, setupSourceMeta, type PickedTools } from '$lib/setupLocalPresentation';
	/** `startConnect` opens on Connect to another computer (from Settings › System), skipping the checks; Back then calls `onLeaveConnect`. `resume` picks up a setup whose account already exists, from the backend's record. */
	interface Props { onExistingAccount: () => void; onAccountCreated: () => void; onFinished: () => void; startConnect?: boolean; onLeaveConnect?: () => void; resume?: boolean }
	const { onExistingAccount, onAccountCreated, onFinished, startConnect = false, onLeaveConnect, resume = false }: Props = $props();
	type Page = 'checking' | 'welcome' | 'account' | 'models' | 'hard-stop' | 'error' | 'connect';
	type SetupStatus = { apple_silicon?: boolean; macos_supported?: boolean; ram_tier?: 'eligible' | 'low' | 'below_minimum'; free_disk_gb?: number; local_eligible?: boolean; app_location?: 'applications' | 'disk_image' | 'other'; work_folder?: string; required_username?: string | null; existing_omlx?: boolean; classifier?: ClassifierState; ready_ai?: string | null; ai_check_pending?: boolean; error?: string };
	type FolderNotice = { kind: 'protected'; folder: string } | { kind: 'unwritable'; reason: string } | null;
	const classifier = createSetupClassifier();
	const connectFlow = createSetupConnect({ token: () => get(token), sources: () => sources, refreshSources: (strict) => refreshSources(strict) });
	const remote = createSetupRemote();
	const canConnect = shellInfo() !== null; // only the desktop app can pair with another computer
	let page = $state<Page>(untrack(() => startConnect) ? 'connect' : 'checking');
	let status = $state<SetupStatus>({});
	const cloud = createSetupPicks({ status: () => status, update: (next) => (status = { ...status, ...next }) });
	const localPick = createSetupLocalPick();
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
	let reportError = $state('');
	let confirmationError = $state('');
	let sources = $state<ModelSource[]>([]);
	let sourceError = $state('');
	let localStartError = $state('');
	let localDownloadPending = $state(false);
	const localActions = createSetupLocalActions({ token: () => get(token), downloading: () => localDownloading, existingOmlx: () => Boolean(status.existing_omlx), refresh: () => refreshSources(),
		askConsent: () => (existingOmlxConsent = true), fail: (message) => (sourceError = message), clearStartError: () => (localStartError = '') });
	let existingOmlxConsent = $state(false);
	/** Welcome's Let CORSIA manage it (or Use mine) for the person's own oMLX. */
	let manageOwnOmlx = $state(true);
	let keySaved = $state(false);
	let providerName = $state('anthropic');

	/** Something besides the local model will run chat: a tool already ready, or one the person checked. */
	const hasSource = $derived(Boolean(status.ready_ai) || cloud.any);
	const noModel = $derived(noLocalModel(status.local_eligible, localPick.pick));

	const steps = $derived<SetupStep[]>(page === 'connect' ? remote.steps : [
		{ label: 'Welcome', icon: 'sparkle', state: page === 'welcome' || page === 'hard-stop' || page === 'checking' ? 'current' : 'done' },
		{ label: 'Your account', icon: 'person', state: page === 'account' ? 'current' : page === 'models' && !existingOmlxConsent ? 'done' : 'todo' },
		{ label: status.ready_ai && page !== 'models' ? status.ready_ai : 'Your AI', icon: 'chip', state: page === 'models' ? 'current' : status.ready_ai ? 'done' : 'todo' },
	]);
	/** Welcome's choice where it showed both (Let CORSIA manage it, Use mine); undefined where it didn't. */
	const ownOmlxChoice = $derived(status.existing_omlx && localPick.wanted ? manageOwnOmlx : undefined);
	const manageExisting = $derived(ownOmlxChoice === true);
	const local = $derived(sources.find((source) => source.source === 'omlx'));
	const localDownloading = $derived(Boolean(local?.setup_downloading || local?.setup_stage || (localDownloadPending && !local?.setup_error)));
	const localJob = $derived(buildLocalJob(local, localDownloading, localPick.pick?.model?.name ?? local?.omlx_management?.starter_name ?? undefined, page, Boolean(get(token)), localStartError, () => void localActions.cancel()));
	const canFinish = $derived(sources.some(sourceIsReady));
	const jobs = $derived<SetupJob[]>([
		...(localJob ? [localJob] : []),
		...(classifier.job ? [classifier.job] : []),
		...(page === 'account' && accountBusy ? [{ label: 'Starting CORSIA', state: 'busy' as const, meta: 'Preparing your account' }] : []),
		...(page === 'error' ? [{ label: 'Starting CORSIA', state: 'error' as const, meta: 'Couldn’t start' }] : []),
		...(page === 'models' && canFinish && !localDownloading ? [{ label: 'Starting CORSIA', state: 'done' as const, meta: 'Ready' }] : []),
	]);
	const modelSize = $derived(localPick.pick?.model ? `${localPick.pick.model.download_size_gb} GB` : local?.download_size || '');
	/** Why this computer got the starter model, said by the server when oMLX recommended none: on the pick, or, for the person's own oMLX under management, on its source row. */
	const starterNote = $derived((localPick.pick?.state === 'picked' && localPick.pick.model ? localPick.pick.message : null) ?? local?.omlx_management?.starter_note ?? undefined);
	const downloadModelLabel = $derived(localPick.pick?.model?.name || 'The local model');
	const usernameError = $derived(username && !/^[a-z0-9_-]{1,32}$/.test(username) ? "Use 1–32 lowercase letters, numbers, '-' or '_'." : '');
	function leaveConnect() { if (startConnect) onLeaveConnect?.(); else page = 'welcome'; }
	type SetupPrimary = { label: string; disabled?: boolean; loading?: boolean; note?: string; noteAlways?: boolean };
	type SetupView = { title: string; lede?: string; primary?: SetupPrimary; onPrimary?: () => void; mark?: boolean };
	const shellView = $derived.by((): SetupView => {
		switch (page) {
			case 'checking':
				return { title: 'Checking this computer', lede: 'This takes a moment.' };
			case 'hard-stop':
				if (status.apple_silicon === false) return { title: 'CORSIA needs Apple silicon', lede: 'This version of CORSIA runs on Macs with Apple silicon.' };
				if (status.macos_supported === false) return { title: 'Update macOS to continue', lede: 'Update to macOS 15 or later, then open CORSIA again.' };
				return { title: 'Move CORSIA to Applications', lede: 'Move CORSIA to Applications, then open it again so updates work.' };
			case 'connect': return remote.view;
			case 'welcome':
				return { title: 'Welcome to CORSIA', lede: 'Your own AI team, on this computer. It remembers your projects, works with the tools and models you already use, and shows you results, not transcripts.', primary: { label: 'Get started', disabled: cloud.detecting || (noModel && !hasSource) }, onPrimary: beginSetup, mark: true };
			case 'account':
				return { title: 'Create your account', primary: { label: status.ready_ai ? 'Start using CORSIA' : 'Continue', disabled: accountBusy || folderChanging || folderNotice?.kind === 'unwritable' || !name.trim() || !username.trim() || !!usernameError || password.length < 8 || !!confirmationError, loading: accountBusy }, onPrimary: createAccount };
			case 'error':
				return { title: 'CORSIA couldn’t start', lede: reportError || statusError || accountError || 'Try again.', primary: { label: 'Try again' }, onPrimary: resume ? resumeSetup : !get(token) ? readStatus : createAccount };
			case 'models':
				if (existingOmlxConsent) return { title: 'Use your oMLX?', lede: 'Your oMLX is already on this computer.' };
				return { title: 'Connect an AI', lede: cloud.any ? 'Setting up what you chose. You can add the others with CORSIA later.' : 'CORSIA needs one to start. You can add the others with CORSIA later.', primary: { label: 'Start using CORSIA', disabled: !canFinish, note: finishNote, noteAlways: Boolean(pickedTools?.failed.length && pickedTools.localReady) }, onPrimary: () => void finishSetupWithBestEffortSelection(get(token), sources, finishSetup) };
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
			if (status.apple_silicon === false || status.macos_supported === false || status.app_location === 'disk_image') page = 'hard-stop';
			else {
				page = 'welcome';
				localPick.open(status.local_eligible !== false, Boolean(status.existing_omlx));
				await cloud.settle();
			}
		} catch {
			statusError = 'CORSIA couldn’t check this computer. Try again, or sign in if you already set it up.';
			page = 'error';
		}
	}

	function togglePick(key: keyof SetupPicks) {
		cloud.toggle(key);
		void recordSetupProgress({ picks: { ...cloud.picks } }, get(token));
		// The local switch is locked on while nothing else will run chat, so unchecking the last pick turns it back on.
		if (!hasSource && !noModel && !localPick.wanted) { localPick.change(true, Boolean(status.existing_omlx)); void recordSetupProgress({ local_ai: true }, get(token)); }
	}

	/** Each checked tool installs, then opens its sign-in by itself. */
	async function connectPickedTools() {
		await Promise.all(cloud.vendors.map((vendor) => {
			const source = sources.find((candidate) => candidate.source === vendor);
			return source ? connectFlow.connectPicked(source) : undefined;
		}));
	}

	type LocalDownloadStart = 'started' | 'consent_required' | 'failed';

	async function startLocalDownload(consentExisting = false): Promise<LocalDownloadStart> {
		if (!localPick.downloads(status.free_disk_gb) && !consentExisting) return 'started';
		const response = await fetch('/api/setup/local-download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ consent_existing: consentExisting, manage_existing: consentExisting ? ownOmlxChoice : undefined }) });
		const result = await response.json().catch(() => null);
		if (response.status === 409 && result?.code === 'consent_required') return 'consent_required';
		return response.ok && result?.ok !== false ? 'started' : 'failed';
	}

	async function beginSetup() {
		void classifier.start();
		if (status.existing_omlx && localPick.wanted) {
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
			if (start === 'started') localDownloadPending = localPick.downloads(status.free_disk_gb);
			else localStartError = 'The local model download couldn’t start.';
		} catch {
			localStartError = 'The local model download couldn’t start.';
		}
		page = 'account';
	}

	/** Finishing is the one thing that closes setup for good: it is recorded before the app opens. */
	const finishSetup = async () => {
		if (await recordSetupProgress({ completed: true }, get(token))) { onFinished(); return; }
		sourceError = 'CORSIA couldn’t save that setup is finished. Try again.'; page = 'models';
	};

	async function decideExistingOmlx(useIt: boolean) {
		void recordSetupProgress({ local_ai: useIt }, get(token));
		if (!useIt) { existingOmlxConsent = false; localPick.wanted = false; page = get(token) ? 'models' : 'account'; return; }
		localActions.busy = true;
		localStartError = '';
		let returnToModels = false;
		try {
			const current = get(token);
			returnToModels = Boolean(current);
			const start = current
				? await setupLocalSource(current, true, ownOmlxChoice)
				: await startLocalDownload(true);
			const started = typeof start === 'string' ? start === 'started' : start.ok;
			if (started) localDownloadPending = true;
			else localStartError = 'The local model couldn’t start.';
		} catch {
			localStartError = 'The local model couldn’t start.';
		} finally {
			localActions.busy = false;
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
			if (folderChanged) { await applyWorkFolder(folder); folderChanged = false; }
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
			await loadCloudProviders();
			password = ''; confirmation = '';
			if (status.ready_ai && canFinish) return finishSetupWithBestEffortSelection(get(token), sources, finishSetup);
			page = 'models';
			void connectPickedTools();
		} catch { accountError = 'CORSIA couldn’t create your account. Try again.'; }
		finally { accountBusy = false; }
	}

	/** The API-key form needs the provider list; if it cannot be read, say so instead of showing an empty choice. */
	async function loadCloudProviders() {
		await loadProviders();
		if (!get(providers).some((provider) => provider.type === 'cloud')) sourceError = 'CORSIA couldn’t load the list of providers. Try again.';
	}

	/** A strict refresh answers a check the person asked for, so a failed recheck is shown, never replaced by the stored rows. */
	async function refreshSources(strict = false) {
		const current = get(token);
		if (!current) return;
		try {
			sources = await (strict ? recheckSources(current) : recheckSources(current).catch(() => fetchSources(current)));
			localDownloadPending = false;
			sourceError = '';
		} catch { sourceError = 'CORSIA couldn’t check your model sources. Try again.'; }
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

	/** After a pick the step lists only what was checked, so no Set up appears for a tool the person did not choose. */
	const harnessSources = $derived(sources.filter(isHarness).filter((source) => !cloud.any || cloud.vendors.includes(source.source)));
	const showLocalRow = $derived(page === 'models' && Boolean(local));
	const aiRows = $derived.by((): AiRow[] => {
		const rows: AiRow[] = harnessSources.map((source) => {
			const row = connectFlow.row(source);
			return { key: source.source, name: VENDOR_COPY[source.source]?.cli ?? source.name, meta: row.meta, busy: row.busy, spinning: row.spinning, error: row.error, detail: row.detail, action: row.action, confirm: row.confirm, more: row.more,
				pill: row.ready ? { state: 'success', label: 'Ready' } : undefined };
		});
		if (keySaved) rows.push({ key: 'api-key', name: 'API key', meta: `Uses your ${$providers.find((provider) => provider.name === providerName)?.label ?? 'provider'} key.`, pill: { state: 'success', label: 'Ready' } });
		if (!showLocalRow || !local) return rows;
		const ready = sourceIsReady(local);
		rows.push({ key: 'omlx', name: 'This computer', meta: setupSourceMeta(local, localStartError, modelSize),
			pill: ready ? { state: 'success', label: 'Ready' } : localDownloading ? { state: 'loading', label: 'Downloading' } : undefined,
			action: ready ? undefined : localDownloading ? { label: 'Cancel', run: localActions.cancel, loading: localActions.busy } : { label: local?.setup_error || localStartError ? 'Try again' : 'Download', run: localActions.retry, loading: localActions.busy },
			error: !localDownloading && !ready && Boolean(local?.setup_error || localStartError), detail: !localDownloading && !ready && (local?.setup_error || localStartError) ? `${downloadModelLabel} didn’t finish downloading. Choose Try again, or use another source.` : starterNote });
		return rows;
	});

	/** The account exists, so the pre-account status gate is closed: the backend's record says where to pick up, and the facts are read fresh. */
	async function resumeSetup() {
		statusError = ''; page = 'checking';
		const progress = await fetchSetupProgress(get(token));
		if (!progress) { statusError = 'CORSIA couldn’t check your setup. Try again.'; page = 'error'; return; }
		if (progress.setup_completed) { onFinished(); return; }
		status = (progress.status ?? {}) as SetupStatus;
		folder = status.work_folder || folder;
		if (status.classifier) classifier.accept(status.classifier);
		localPick.wanted = progress.local_ai ?? status.local_eligible !== false;
		if (progress.picks) cloud.restore(progress.picks);
		existingOmlxConsent = progress.step === 'local-consent'; page = 'models';
		await Promise.all([refreshSources(), classifier.read()]);
		await loadCloudProviders();
	}

	$effect(() => {
		if (page === 'models' && get(token)) void recordSetupProgress({ step: existingOmlxConsent ? 'local-consent' : 'models' }, get(token));
	});

	/** The rows on the step that the wait line and the local-model note speak to: only rows the screen shows. */
	const pickedTools = $derived.by((): PickedTools | undefined => {
		const rows = cloud.vendors.map((vendor) => aiRows.find((row) => row.key === vendor)).filter((row): row is AiRow => Boolean(row) && !row?.pill);
		const keyOpen = cloud.picks.key && !keySaved;
		if (!rows.length && !keyOpen) return undefined;
		const names = (keep: (row: AiRow) => boolean) => rows.filter(keep).map((row) => row.name);
		return { waiting: names((row) => !row.error && Boolean(row.busy)), idle: names((row) => !row.error && !row.busy), failed: names((row) => Boolean(row.error)), localReady: Boolean(local && sourceIsReady(local)), keyOpen };
	});

	const finishNote = $derived(setupFinishNote(local, localDownloading, localStartError, pickedTools));

	$effect(() => (page === 'connect' ? remote.start() : undefined));
	onMount(() => {
		if (startConnect) { /* Connect opened from Settings: no checks to run */ }
		else if (resume) void resumeSetup();
		else void readStatus();
		const offModels = on('models_updated', () => { if (page === 'models') void refreshSources(); });
		const offClassifier = on('classifier_updated', () => { void classifier.read(); });
		const onVisible = () => { if (document.visibilityState === 'visible') void recheckOnFocus(); };
		const onFocus = () => { void classifier.recheckSpace(); void recheckOnFocus(); };
		window.addEventListener('focus', onFocus);
		document.addEventListener('visibilitychange', onVisible);
		return () => { offModels(); offClassifier(); window.removeEventListener('focus', onFocus); document.removeEventListener('visibilitychange', onVisible); };
	});
</script>

<SetupShell {steps} section={page === 'connect' ? 'Connect' : 'Setup'} {jobs} page={page === 'connect' ? `connect-${remote.stage}` : page} title={shellView.title} lede={shellView.lede} primary={shellView.primary} mark={shellView.mark} onPrimary={shellView.onPrimary}>
	{#snippet secondary()}
		{#if page === 'connect'}
			<SetupConnectBack onclick={() => (remote.stage === 'code' ? remote.toChoose() : leaveConnect())} />
		{:else if page === 'welcome' && canConnect}
			<Button variant="chip" onclick={() => { remote.reset(); page = 'connect'; }}>Connect to CORSIA on another computer</Button>
		{:else if page === 'hard-stop'}
			<Button variant="chip" onclick={readStatus}>Check again</Button>
		{:else if page === 'error' && setupReportInvoke()}
			<Button variant="chip" onclick={async () => { reportError = await saveSetupReport(); }}>Save a report</Button>
		{/if}
	{/snippet}

	{#if page === 'connect'}
		<SetupConnect {remote} />
	{:else if page === 'welcome'}
		<SetupWelcomeChoice localEligible={status.local_eligible} freeDiskGb={status.free_disk_gb} pick={localPick.pick} downloadEtaSeconds={local?.download_eta_seconds} downloadStalled={local?.download_stalled} wanted={localPick.wanted}
			onWantedChange={(next) => { localPick.change(next, Boolean(status.existing_omlx)); void recordSetupProgress({ local_ai: next }, get(token)); }} onRecheck={readStatus} onRetry={() => void localPick.start()}
			ownOmlx={Boolean(status.existing_omlx)} manage={manageOwnOmlx} onManageChange={(next) => (manageOwnOmlx = next)}
			picks={cloud.picks} onTogglePick={togglePick} ready={cloud.questionShown ? null : status.ready_ai ?? null} detecting={cloud.detecting} />
	{:else if page === 'account'}
		<SetupAccountForm bind:folder bind:folderChanged bind:name bind:username bind:password bind:confirmation bind:email {usernameError} {confirmationError} requiredUsername={status.required_username} {accountError} {accountBusy} bind:folderChanging bind:folderNotice onSubmit={createAccount} onValidateConfirmation={validateConfirmation} />
	{:else if page === 'models' && existingOmlxConsent}
		<SetupExistingOmlxConsent manage={manageExisting} busy={localActions.busy} onDecide={decideExistingOmlx} />
	{:else if page === 'models'}
		<div class="sources">
			{#if cloud.picks.key && !keySaved}
				<SetupApiKeyRow bind:providerName onError={(message) => (sourceError = message)} onSaved={async () => { keySaved = true; await loadProviders(); await refreshSources(); }} />
			{/if}
			<SetupAiRows rows={aiRows} />
			{#if sourceError}<Message variant="err">{sourceError}</Message>{/if}
		</div>
	{/if}
</SetupShell>

<style>
	/* Account form styles live with SetupAccountForm. */
	.sources { display: flex; flex-direction: column; gap: var(--space-4); max-width: calc(var(--space-8) * 9); color: var(--text-secondary); }
</style>
