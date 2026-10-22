<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { get } from 'svelte/store';
	import { Button } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from './SetupShell.svelte';
	import SetupAiRows, { type AiRow } from './SetupAiRows.svelte';
	import SetupAccountForm from './SetupAccountForm.svelte';
	import SetupWelcome from './SetupWelcome.svelte';
	import SetupWorkFolder, { type FolderNotice } from './SetupWorkFolder.svelte';
	import SetupConnect from './SetupConnect.svelte';
	import SetupConnectBack from './SetupConnectBack.svelte';
	import SetupSubscriptions, { subscriptionQuestion, type SetupPicks } from './SetupSubscriptions.svelte';
	import AddProviderModal from './v7/AddProviderModal.svelte';
	import ProviderSettingsModal from './v7/ProviderSettingsModal.svelte';
	import type { HostProvider } from '$lib/stores/providersV7';
	import { token, user } from '$lib/stores/auth';
	import { fetchSources, recheckSources, cancelLocalDownload, sourceIsReady, type ModelSource } from '$lib/conductorSources';
	import { stopLocalServer } from '$lib/conductorSourcesApi';
	import { createSetupConnect, POLL_MS } from '$lib/setupConnect.svelte';
	import { loadProviders, testedProviders, type ProviderEntry } from '$lib/stores/providers';
	import { on } from '$lib/ws/events';
	import { connect } from '$lib/ws';
	import { bestSetupModel, setupModelWorks, setupCatalog, bindSetupFirstModel, SetupFirstModelError, type SetupModel } from '$lib/setupFirstModel';
	import { fetchSetupProgress, recordSetupProgress } from '$lib/setupProgress';
	import { saveSetupReport, type SetupReportRow } from '$lib/setupReport';
	import { createSetupClassifier, type ClassifierState } from '$lib/setupClassifier.svelte';
	import { createSetupLocalPick } from '$lib/setupLocalPick.svelte';
	import { createSetupRemote } from '$lib/setupRemote.svelte';
	import { applyWorkFolder, shellInfo } from '$lib/remoteShell';
	import { setupLocalRow, setupStartNote, type LocalRowState } from '$lib/setupProviderRows';
	import { measuredDownloadTime } from '$lib/setupLocalPresentation';
	import { WELCOME_ACCOUNT_LINE, WELCOME_TITLE } from '$lib/setupWelcome';
	import { isSetupWide } from '$lib/stores/viewport';
	interface Props { onExistingAccount: () => void; onAccountCreated: () => void; onFinished: () => void; startConnect?: boolean; onLeaveConnect?: () => void; resume?: boolean }
	type LocalStartResult = 'started' | 'failed';
	type LocalStartResponse = { result: LocalStartResult; reference?: string; fix?: string };
	const LOCAL_START_NETWORK_FAILURE = 'CORSIA couldn’t start local setup.';
	async function requestLocalStart(session: string | null): Promise<LocalStartResponse> {
		const response = await fetch(session ? '/api/providers/local/setup' : '/api/setup/local-download', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', ...(session ? { Authorization: `Bearer ${session}` } : {}) },
			body: JSON.stringify(session ? { onboarding: true } : {}),
		});
		const body = await response.json().catch(() => null);
		const reference = typeof body?.detail?.ref === 'string' ? body.detail.ref : typeof body?.ref === 'string' ? body.ref : undefined;
		const fix = typeof body?.detail?.fix === 'string' ? body.detail.fix : typeof body?.fix === 'string' ? body.fix : undefined;
		if (response.ok && body?.ok !== false) return { result: 'started', reference };
		if (!fix) throw new Error('local start response omitted recovery');
		return { result: 'failed', reference, fix };
	}
	function setupFinishRecovery(error: unknown): string {
		const code = error instanceof SetupFirstModelError ? error.code : undefined;
		if (code === 'no_model') return 'CORSIA couldn’t find a working model. Connect a provider or choose one that is ready, then try again.';
		if (code === 'binding_incomplete') return 'CORSIA couldn’t save the selected model. Try again.';
		if (code === 'catalog_refused') return 'CORSIA couldn’t save the selected model. Check the provider setup, then try again.';
		if (code === 'catalog_unavailable') return 'CORSIA couldn’t check your available models. Try again.';
		if (code === 'agent_unavailable') return 'CORSIA couldn’t reach its agent. Try again.';
		if (code === 'progress_save') return 'CORSIA couldn’t save setup progress. Try again.';
		return 'CORSIA couldn’t finish model setup. Try again.';
	}
	/** The backend's own account refusal names the cause; a validation 422 has no string detail. */
	function accountRefusal(detail: unknown): string | null {
		return typeof detail === 'string' && detail.trim() ? detail.trim() : null;
	}
	const { onExistingAccount, onAccountCreated, onFinished, startConnect = false, onLeaveConnect, resume = false }: Props = $props();
	type Page = 'checking' | 'account' | 'subscriptions' | 'models' | 'hard-stop' | 'error' | 'connect';
	const CLI_VENDORS = ['claude-code', 'codex'] as const;
	type Status = { apple_silicon?: boolean; macos_supported?: boolean; free_disk_gb?: number; local_eligible?: boolean; work_folder?: string; required_username?: string | null; classifier?: ClassifierState };
	const classifier = createSetupClassifier(), localPick = createSetupLocalPick(), remote = createSetupRemote();
	let page = $state<Page>(untrack(() => startConnect) ? 'connect' : 'checking');
	// Below 1280 wide, Welcome is its own screen before the account form; from 1280 it sits beside the form instead.
	let welcomeSeen = $state(false);
	const screen = $derived(page === 'account' && !$isSetupWide && !welcomeSeen && !$token ? 'welcome' : page);
	let status = $state<Status>({}), statusError = $state(''), reportError = $state('');
	let name = $state(''), username = $state(''), password = $state(''), confirmation = $state(''), email = $state('');
	let folder = $state('~/CORSIA'), folderChanged = $state(false), folderChanging = $state(false), folderNotice = $state<FolderNotice>(null);
	let accountBusy = $state(false), accountError = $state(''), accountExists = $state(false), confirmationError = $state('');
	let sources = $state<ModelSource[]>([]), catalog = $state<SetupModel[]>([]), sourceError = $state('');
	let localStartError = $state<'other' | ''>(''), localStartReference = $state(''), startFix = $state<string | undefined>(), localBusy = $state(false);
	// The pre-start (pick, then start) is under way; the person turned the download off, so setup does not restart it by itself.
	let localPreparing = $state(false), modelsAutoStarted = false;
	let finishing = $state(false), showAdd = $state(false), openProvider = $state<HostProvider | null>(null);
	// The subscriptions answer; null until answered (or skipped, or a resumed setup), when every CLI row shows.
	let picks = $state<SetupPicks | null>(null), confirmLocalStop = $state(false), localOffFailed = $state<{ reference?: string } | undefined>();
	const connectFlow = createSetupConnect({ token: () => get(token), sources: () => sources, refreshSources });
	const canConnect = shellInfo() !== null;
	let preauthLocal = $state<LocalRowState | undefined>();
	const local = $derived(($token ? sources.find((source) => source.source === 'omlx') : preauthLocal) as LocalRowState | undefined);
	const eligible = $derived(status.local_eligible !== false);
	const downloading = $derived(['downloading', 'paused', 'starting'].includes(local?.local_state ?? ''));
	const localFailed = $derived(['failed', 'setup_failed', 'start_failed'].includes(local?.local_state ?? '') || Boolean(localStartError));
	const tested = $derived($testedProviders.map((source) => ({ source })));
	const best = $derived(bestSetupModel(catalog, tested));
	const canFinish = $derived(Boolean(best));
	const cliSignedIn = (vendor: string) => { const source = sources.find((row) => row.source === vendor); return Boolean(source && (sourceIsReady(source) || source.limited)); };
	const cliPicked = (vendor: string) => picks === null || (vendor === 'codex' ? picks.chatgpt : picks.claude);
	const subscriptionsSkipped = $derived(subscriptionQuestion(cliSignedIn('claude-code'), cliSignedIn('codex')) === null);
	// A working cloud model frees the local download to be turned off.
	const cloudReady = $derived(catalog.some((row) => row.connection !== 'omlx' && setupModelWorks(row)));
	const usernameError = $derived(username && !/^[a-z0-9_-]{1,32}$/.test(username) ? 'Use up to 32 lowercase letters, numbers, dashes or underscores.' : '');
	const accountName = $derived(username.trim() || $user?.name || '');
	const accountValid = $derived(Boolean(name.trim() && username.trim() && !usernameError && password.length >= 8 && confirmation === password));
	const steps = $derived<SetupStep[]>(page === 'connect' ? remote.steps : [
		{ label: 'Your account', icon: 'person', state: ['checking', 'account', 'hard-stop'].includes(page) || (page === 'error' && !$token) ? 'current' : 'done' },
		{ label: 'Your subscriptions', icon: 'sparkle', state: page === 'subscriptions' ? 'current' : page === 'models' ? 'done' : 'todo' },
		{ label: 'Providers & models', icon: 'chip', state: page === 'models' ? 'current' : 'todo' },
	]);
	/** Every failed row's cause and reference, so the saved report says what went wrong where. */
	function reportRows(): SetupReportRow[] {
		return aiRows.filter((row) => row.error).map((row) => ({
			key: row.key as SetupReportRow['key'], name: row.name, reference: row.reference,
			cause: row.key === 'omlx' ? (localOffFailed ? 'turn_off_failed' : local?.setup_error_code ?? (localStartError || undefined)) : connectFlow.attempts[row.key]?.cause,
		}));
	}
	/** Save a report only where a reference gives the report log lines to find; the row's own action covers the rest. */
	function withReportAction(row: AiRow): AiRow {
		return row.reportable && row.reference ? { ...row, more: [...(row.more ?? []), { label: 'Save a report', run: () => void saveSetupReport(reportRows()) }] } : row;
	}
	const jobs = $derived<SetupJob[]>([
		...(page !== 'models' && (downloading || localFailed) ? [{ label: 'Local model', state: localFailed ? 'error' as const : 'busy' as const,
			meta: localFailed ? (localStartError ? startFix : local?.fix) ?? '' : local?.local_state === 'starting' ? 'Starting the local model…' : ['Downloading', `${local?.download_percent ?? 0}%`, measuredDownloadTime(local?.download_eta_seconds)].filter(Boolean).join(' · '), progress: (local?.download_percent ?? 0) / 100 }] : []),
		...(classifier.job ? [classifier.job] : []),
		...(accountBusy ? [{ label: 'Starting CORSIA', state: 'busy' as const }] : []),
	]);
	const aiRows = $derived.by((): AiRow[] => {
		const rows = CLI_VENDORS.filter((vendor) => cliSignedIn(vendor) || cliPicked(vendor)).map((vendor): AiRow => {
			const source = sources.find((row) => row.source === vendor);
			if (!source) {
				const name = vendor === 'codex' ? 'Codex' : 'Claude Code';
				return sourceError ? { key: vendor, name, meta: `Couldn’t check ${name}.`, error: true, action: { label: 'Retry', run: () => void refreshSources(true) } } : { key: vendor, name, meta: '', busy: true };
			}
			const view = connectFlow.row(source);
			const works = catalog.some((row) => row.connection === vendor && setupModelWorks(row));
			return withReportAction({ key: vendor, name: vendor === 'codex' ? 'Codex' : 'Claude Code', ...view, pill: works ? { state: 'success', label: 'Ready' } : undefined });
		});
		rows.push(withReportAction(setupLocalRow({ source: local, pick: localPick.pick, eligible, downloading, preparing: localPreparing || localPick.pick?.state === 'checking', ready: catalog.some((row) => row.connection === 'omlx' && setupModelWorks(row)), startError: localStartError, startFix, startReference: localStartReference, freeDisk: status.free_disk_gb,
			cloudReady, confirmingStop: confirmLocalStop, turnOffFailed: localOffFailed,
			start: () => void startLocal(), stop: () => void stopLocal(), check: () => void checkLocal(),
			confirmStop: (next) => { confirmLocalStop = next; }, turnOff: () => void turnOffLocal() })));
		const provider = tested.find((test) => bestSetupModel(catalog.filter((row) => row.connection !== 'omlx' && row.connection !== 'claude-code' && row.connection !== 'codex'), [test]));
		rows.push({ key: 'api', name: 'API provider', meta: provider ? `${provider.source} · passed a test call` : 'Pay as you go, with a key from Anthropic, OpenAI or another provider.',
			pill: provider ? { state: 'success', label: 'Ready' } : undefined, action: provider ? undefined : { label: 'Add provider', run: () => { showAdd = true; } } });
		return rows;
	});
	const finishNote = $derived(setupStartNote(canFinish, eligible, downloading || localPreparing, local?.download_eta_seconds));
	type View = { title: string; lede?: string; primary?: { label: string; disabled?: boolean; loading?: boolean; note?: string; noteAlways?: boolean }; onPrimary?: () => void };
	const view = $derived.by((): View => {
		if (page === 'connect') return remote.view;
		if (page === 'checking') return { title: 'Checking this computer' };
		if (page === 'error') return { title: 'CORSIA couldn’t start', lede: reportError || statusError || 'Something went wrong while starting. Try again, or save a report to send.', primary: { label: 'Retry' }, onPrimary: resume ? resumeSetup : readStatus };
		if (page === 'hard-stop') return { title: 'Update macOS to open CORSIA', lede: 'CORSIA needs macOS 15 or later. Update in System Settings › General › Software Update, then open CORSIA again.' };
		if (screen === 'welcome') return { title: WELCOME_TITLE, primary: { label: 'Continue' }, onPrimary: () => { welcomeSeen = true; } };
		// Once the account exists, the screen reads back what was set: neither the account nor its folder can change after it.
		if (page === 'account' && $token) return { title: 'Your account', primary: { label: 'Continue' }, onPrimary: afterAccount };
		if (page === 'account') return { title: $isSetupWide ? WELCOME_TITLE : 'Create your account', lede: $isSetupWide ? WELCOME_ACCOUNT_LINE : undefined, primary: { label: 'Continue', loading: accountBusy, disabled: !accountValid || accountBusy || folderChanging || folderNotice?.kind === 'unwritable' || folderNotice?.kind === 'picker_failed' }, onPrimary: createFromForm };
		if (page === 'subscriptions') return { title: 'Your subscriptions', lede: 'CORSIA’s agents can work through a Claude or ChatGPT plan you already pay for.' };
		return { title: 'Providers & models', lede: 'CORSIA needs one working model to continue setup.', primary: { label: 'Start using CORSIA', disabled: !canFinish || finishing, loading: finishing, note: finishNote, noteAlways: true }, onPrimary: finishSetup };
	});
	function validateConfirmation() { confirmationError = confirmation && password !== confirmation ? 'Passwords don’t match.' : ''; }
	/** Continue on the account screen creates the account, applying the chosen folder first. */
	async function createFromForm() { validateConfirmation(); if (accountValid && !get(token)) await createAccount(); }
	/** The subscriptions screen is asked only while a CLI is still not signed in. */
	function afterAccount() { page = subscriptionsSkipped ? 'models' : 'subscriptions'; }
	/** One answer: picked tools start installing now, an unpicked one stops what it started. */
	function answerSubscriptions(next: SetupPicks) {
		picks = next;
		// Kept so the first chat knows which subscriptions the person said they pay for.
		void recordSetupProgress({ picks: next }, get(token));
		for (const vendor of CLI_VENDORS) {
			if (cliSignedIn(vendor)) continue;
			const source = sources.find((row) => row.source === vendor);
			if (cliPicked(vendor)) { if (source) void connectFlow.install(source); }
			else void connectFlow.stop(vendor);
		}
		page = 'models';
	}
	function leaveConnect() { if (startConnect) onLeaveConnect?.(); else page = 'account'; }
	async function readStatus() {
		page = 'checking'; statusError = '';
		try {
			const response = await fetch('/api/setup/status');
			if (response.status === 409) { onExistingAccount(); return; }
			if (!response.ok) throw new Error('status');
			status = await response.json(); folder = status.work_folder || folder;
			if (status.required_username) username = status.required_username;
			if (status.classifier) classifier.accept(status.classifier);
			if (status.macos_supported === false) { page = 'hard-stop'; return; }
			page = 'account'; void classifier.start();
			if (eligible) {
				localPreparing = true;
				try { await localPick.start(); await startLocal(); await readLocalProgress(); } finally { localPreparing = false; }
			}
		} catch { statusError = 'Something went wrong while starting. Try again, or save a report to send.'; page = 'error'; }
	}
	async function checkLocal() {
		const current = get(token);
		if (current) {
			const progress = await fetchSetupProgress(current);
			if (progress?.status) status = progress.status as Status;
			await startLocal();
			return;
		}
		await localPick.start();
		await readLocalProgress();
	}
	async function readLocalProgress() {
		try {
			const response = await fetch('/api/setup/local-download');
			if (!response.ok) return;
			const live = await response.json();
			preauthLocal = { source: 'omlx', name: live.model ?? 'Local model', state: live.state, local_state: live.state,
				download_percent: live.percent, download_eta_seconds: live.eta_seconds, download_size: live.download_size, download_stalled: live.stalled,
				setup_error_code: live.cause, setup_error_reference: live.ref, bytes_to_free: live.bytes_to_free, fix: live.fix };
		} catch { /* Preserve the last job projection until the next successful read. */ }
	}
	async function startLocal() {
		if (!eligible || localBusy) return;
		localBusy = true; localStartError = ''; localStartReference = ''; startFix = undefined;
		try {
			const current = get(token);
			const { result, reference, fix } = await requestLocalStart(current);
			localStartReference = reference ?? '';
			if (result === 'failed') { localStartError = 'other'; startFix = fix; }
			if (current) await refreshSources();
		} catch { localStartError = 'other'; startFix = LOCAL_START_NETWORK_FAILURE; }
		finally { localBusy = false; }
	}
	async function stopLocal() {
		const current = get(token);
		if (current) { await cancelLocalDownload(current); await refreshSources(); }
		else await fetch('/api/setup/local-download/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
	}
	async function turnOffLocal() {
		const current = get(token);
		if (!current) return;
		const stopped = await stopLocalServer(current).catch(() => ({ ok: false, ref: undefined }));
		localOffFailed = stopped.ok ? undefined : { reference: stopped.ref };
		await refreshSources();
	}
	async function connectToAnotherComputer() {
		await stopLocal();
		remote.reset(); page = 'connect';
	}
	async function createAccount() {
		if (!accountValid || accountBusy) return;
		accountBusy = true; accountError = '';
		try {
			if (folderChanged) { await applyWorkFolder(folder); folderChanged = false; }
			const response = await fetch('/api/setup/first-account', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim(), username: username.trim(), password, email: email.trim() || null }) });
			if (response.status === 409) { accountExists = true; accountError = 'This computer already has a CORSIA account. Sign in instead.'; page = 'account'; return; }
			const result = await response.json().catch(() => null);
			if (!response.ok) {
				if (response.status === 422) { page = 'account'; accountError = accountRefusal(result?.detail) ?? 'Couldn’t create your account. Try again.'; }
				else accountError = 'Couldn’t create your account. Try again.';
				return;
			}
			onAccountCreated(); token.set(result.token); user.set(result.user ?? { id: result.user_id, name: name.trim() }); connect();
			password = ''; confirmation = '';
			await Promise.all([refreshSources(), classifier.read(), loadProviders()]);
			afterAccount();
		} catch { accountError = 'Couldn’t create your account. Try again.'; }
		finally { accountBusy = false; }
	}
	async function refreshSources(strict = false) {
		const current = get(token); if (!current) return;
		try {
			sources = await (strict ? recheckSources(current) : fetchSources(current));
			catalog = (await setupCatalog(current)).models;
			sourceError = '';
		} catch { catalog = []; sourceError = 'CORSIA couldn’t check your model sources. Try again.'; }
	}
	async function finishSetup() {
		const current = get(token); if (!current || finishing) return;
		finishing = true; sourceError = '';
		try {
			await bindSetupFirstModel(current, tested, async () => {
				if (!await recordSetupProgress({ handoff: { tools: connectFlow.attempts } }, current)) throw new SetupFirstModelError('progress_save');
			});
			// The answer rides the completion write too, so a failed earlier save is repaired before the first chat reads it.
			if (!await recordSetupProgress({ completed: true, handoff: { tools: connectFlow.attempts }, ...(picks ? { picks } : {}) }, current)) throw new SetupFirstModelError('progress_save');
			onFinished();
		} catch (error) {
			await refreshSources(true);
			sourceError = setupFinishRecovery(error);
		}
		finally { finishing = false; }
	}
	async function resumeSetup() {
		const progress = await fetchSetupProgress(get(token));
		if (!progress) { page = 'error'; return; }
		if (progress.setup_completed) { onFinished(); return; }
		status = (progress.status ?? {}) as Status; folder = status.work_folder || folder; page = 'models';
		if (status.classifier) classifier.accept(status.classifier);
		await Promise.all([refreshSources(true), classifier.read(), loadProviders()]);
	}
	function selectProvider(provider: ProviderEntry) { showAdd = false; openProvider = { name: provider.name, label: provider.label, type: provider.type, statusDot: 'disabled', subline: '', raw: provider }; }
	$effect(() => {
		// Authenticated source/catalog state arrives through models_updated. Poll only the two setup-owned jobs
		// that have no event while running: CLI install/sign-in, and the pre-account local downloader.
		if (!connectFlow.active && (!downloading || get(token))) return;
		const timer = setInterval(() => {
			if (connectFlow.active) void connectFlow.tick();
			if (!get(token) && downloading) void readLocalProgress();
		}, POLL_MS);
		return () => clearInterval(timer);
	});
	$effect(() => (page === 'connect' ? remote.start() : undefined));
	// Providers & models opening with nothing running or in flight starts the download itself, once per opening.
	$effect(() => {
		if (page !== 'models') { modelsAutoStarted = false; return; }
		if (modelsAutoStarted || !$token || !eligible || localPreparing || localBusy || localStartError) return;
		if (!local || local.local_state !== 'stopped' || local.local_cancelled || local.reason_code === 'not_running') return;
		modelsAutoStarted = true; void startLocal();
	});
	onMount(() => {
		if (!startConnect) void (resume ? resumeSetup() : readStatus());
		const offModels = on('models_updated', () => { if (page === 'models') void refreshSources(); });
		const offClassifier = on('classifier_updated', () => { void classifier.read(); });
		const focus = () => { if (page === 'models') void refreshSources(true); };
		window.addEventListener('focus', focus);
		return () => { offModels(); offClassifier(); window.removeEventListener('focus', focus); };
	});
</script>

{#snippet welcomeAside()}<SetupWelcome accountLine={false} />{/snippet}
<SetupShell {steps} section={page === 'connect' ? 'Connect' : 'Setup'} {jobs} page={page === 'connect' ? `connect-${remote.stage}` : screen} title={view.title} lede={view.lede} primary={view.primary} aside={page === 'account' && $isSetupWide && !$token ? welcomeAside : undefined} onPrimary={view.onPrimary}>
	{#snippet secondary()}
		{#if page === 'connect'}<SetupConnectBack onclick={() => remote.stage === 'code' ? remote.toChoose() : leaveConnect()} />
		{:else if (screen === 'welcome' || page === 'account') && canConnect && !$token}<Button variant="chip" onclick={() => void connectToAnotherComputer()}>Connect to CORSIA on another computer</Button>{#if accountError && page === 'account'}<Button variant="chip" onclick={async () => { accountError = await saveSetupReport(); }}>Save a report</Button>{/if}
		{:else if page === 'account' && accountError}<Button variant="chip" onclick={async () => { accountError = await saveSetupReport(); }}>Save a report</Button>
		{:else if page === 'subscriptions'}<Button variant="chip" onclick={() => { page = 'account'; }}>Back</Button>
		{:else if page === 'models'}<Button variant="chip" disabled={finishing} onclick={() => { page = subscriptionsSkipped ? 'account' : 'subscriptions'; }}>Back</Button>
		{:else if page === 'hard-stop'}<Button variant="chip" onclick={readStatus}>Check again</Button>
		{:else if page === 'error'}<Button variant="chip" onclick={async () => { reportError = await saveSetupReport(); }}>Save a report</Button>{/if}
	{/snippet}
	{#if page === 'connect'}<SetupConnect {remote} />
	{:else if screen === 'welcome'}<SetupWelcome />
	{:else if page === 'account' && $token}
		<p class="settled" data-testid="setup-account-made">{accountName ? `Your account is set up as ${accountName}.` : 'Your account is set up.'}</p>
		<p class="settled" data-testid="setup-folder-made">CORSIA keeps your work in {folder}.</p>
	{:else if page === 'account'}
		<SetupAccountForm bind:name bind:username bind:password bind:confirmation bind:email {usernameError} {confirmationError} requiredUsername={status.required_username} {accountError} onSubmit={createFromForm} onValidateConfirmation={validateConfirmation} />
		<SetupWorkFolder bind:folder bind:changed={folderChanged} bind:changing={folderChanging} bind:notice={folderNotice} />
		{#if accountExists}<Button variant="chip" onclick={onExistingAccount}>Sign in</Button>{/if}
	{:else if page === 'subscriptions'}<SetupSubscriptions claudeReady={cliSignedIn('claude-code')} codexReady={cliSignedIn('codex')} {eligible} answer={picks} onAnswer={answerSubscriptions} />
	{:else if page === 'models'}
		<SetupAiRows rows={aiRows} />
		{#if sourceError}<p class="error-text" role="alert">{sourceError}</p><Button variant="chip" onclick={() => void refreshSources(true)}>Retry</Button>{/if}
	{/if}
</SetupShell>
{#if showAdd}<AddProviderModal onClose={() => { showAdd = false; void refreshSources(); }} onSelectCloud={selectProvider} />{/if}
{#if openProvider}<ProviderSettingsModal provider={openProvider} initialTab="advanced" onClose={() => { openProvider = null; void refreshSources(); }} />{/if}

<style>
	p { font-size: var(--text-footnote); line-height: var(--lh-normal); }
	.error-text { margin: var(--space-2) var(--space-4); }
	.settled { margin: 0; font-size: var(--text-body); color: var(--text-primary); }
</style>
