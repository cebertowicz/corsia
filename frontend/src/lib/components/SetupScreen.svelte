<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { get } from 'svelte/store';
	import { Button, Message } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from './SetupShell.svelte';
	import SetupAiRows, { type AiRow } from './SetupAiRows.svelte';
	import SetupAccountForm from './SetupAccountForm.svelte';
	import SetupWelcome from './SetupWelcome.svelte';
	import SetupWorkFolder, { type FolderNotice } from './SetupWorkFolder.svelte';
	import SetupConnect from './SetupConnect.svelte';
	import SetupConnectBack from './SetupConnectBack.svelte';
	import AddProviderModal from './v7/AddProviderModal.svelte';
	import ProviderSettingsModal from './v7/ProviderSettingsModal.svelte';
	import type { HostProvider } from '$lib/stores/providersV7';
	import { token, user } from '$lib/stores/auth';
	import { fetchSources, recheckSources, cancelLocalDownload, type ModelSource } from '$lib/conductorSources';
	import { createSetupConnect, POLL_MS } from '$lib/setupConnect.svelte';
	import { loadProviders, testedProviders, type ProviderEntry } from '$lib/stores/providers';
	import { on } from '$lib/ws/events';
	import { connect } from '$lib/ws';
	import { bestSetupModel, setupModelWorks, setupCatalog, bindSetupFirstModel, SetupFirstModelError, type SetupModel } from '$lib/setupFirstModel';
	import { fetchSetupProgress, recordSetupProgress } from '$lib/setupProgress';
	import { saveSetupReport } from '$lib/setupReport';
	import { createSetupClassifier, type ClassifierState } from '$lib/setupClassifier.svelte';
	import { createSetupLocalPick } from '$lib/setupLocalPick.svelte';
	import { createSetupRemote } from '$lib/setupRemote.svelte';
	import { applyWorkFolder, shellInfo } from '$lib/remoteShell';
	import { setupLocalRow, setupStartNote, type LocalRowState } from '$lib/setupProviderRows';
	import { measuredDownloadTime } from '$lib/setupLocalPresentation';
	import { WELCOME_ACCOUNT_LINE, WELCOME_TITLE } from '$lib/setupWelcome';
	import { isSetupWide } from '$lib/stores/viewport';
	interface Props { onExistingAccount: () => void; onAccountCreated: () => void; onFinished: () => void; startConnect?: boolean; onLeaveConnect?: () => void; resume?: boolean }
	type LocalStartResult = 'started' | 'consent_required' | 'foreign_omlx_changed' | 'failed';
	type LocalStartResponse = { result: LocalStartResult; reference?: string; fix?: string };
	const LOCAL_START_NETWORK_FAILURE = 'CORSIA couldn’t start local setup.';
	async function requestLocalStart(session: string | null, otherOmlx: 'stop' | 'keep' | null = null): Promise<LocalStartResponse> {
		const response = await fetch(session ? '/api/providers/local/setup' : '/api/setup/local-download', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', ...(session ? { Authorization: `Bearer ${session}` } : {}) },
			body: JSON.stringify({ other_omlx: otherOmlx, ...(session ? { onboarding: true } : {}) }),
		});
		const body = await response.json().catch(() => null);
		const code = body?.detail?.code ?? body?.code;
		const reference = typeof body?.detail?.ref === 'string' ? body.detail.ref : typeof body?.ref === 'string' ? body.ref : undefined;
		const fix = typeof body?.detail?.fix === 'string' ? body.detail.fix : typeof body?.fix === 'string' ? body.fix : undefined;
		if (response.status === 409 && code === 'consent_required') return { result: 'consent_required', reference };
		if (response.status === 409 && code === 'foreign_omlx_changed') {
			if (!fix) throw new Error('local start response omitted recovery');
			return { result: 'foreign_omlx_changed', reference, fix };
		}
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
	const { onExistingAccount, onAccountCreated, onFinished, startConnect = false, onLeaveConnect, resume = false }: Props = $props();
	type Page = 'checking' | 'account' | 'folder' | 'models' | 'hard-stop' | 'error' | 'connect';
	type Status = { apple_silicon?: boolean; macos_supported?: boolean; free_disk_gb?: number; local_eligible?: boolean; app_location?: string; work_folder?: string; required_username?: string | null; classifier?: ClassifierState };
	const classifier = createSetupClassifier(), localPick = createSetupLocalPick(), remote = createSetupRemote();
	let page = $state<Page>(untrack(() => startConnect) ? 'connect' : 'checking');
	// Below 1280 wide, Welcome is its own screen before the account form; from 1280 it sits beside the form instead.
	let welcomeSeen = $state(false);
	const screen = $derived(page === 'account' && !$isSetupWide && !welcomeSeen ? 'welcome' : page);
	let status = $state<Status>({}), statusError = $state(''), reportError = $state('');
	let name = $state(''), username = $state(''), password = $state(''), confirmation = $state(''), email = $state('');
	let folder = $state('~/CORSIA'), folderChanged = $state(false), folderChanging = $state(false), folderNotice = $state<FolderNotice>(null);
	let accountBusy = $state(false), accountError = $state(''), accountUsernameError = $state(''), accountExists = $state(false), confirmationError = $state('');
	let sources = $state<ModelSource[]>([]), catalog = $state<SetupModel[]>([]), sourceError = $state('');
	let localStartError = $state<'other' | 'other_omlx' | ''>(''), localStartReference = $state(''), startFix = $state<string | undefined>(), localBusy = $state(false), otherQuestion = $state(false);
	let finishing = $state(false), showAdd = $state(false), openProvider = $state<HostProvider | null>(null);
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
	const usernameError = $derived(accountUsernameError || (username && !/^[a-z0-9_-]{1,32}$/.test(username) ? 'Use up to 32 lowercase letters, numbers, dashes or underscores.' : ''));
	const accountValid = $derived(Boolean(name.trim() && username.trim() && !usernameError && password.length >= 8 && confirmation === password));
	const steps = $derived<SetupStep[]>(page === 'connect' ? remote.steps : [
		{ label: 'Your account', icon: 'person', state: ['checking', 'account', 'hard-stop'].includes(page) ? 'current' : 'done' },
		{ label: 'Work folder', icon: 'computer', state: page === 'folder' ? 'current' : page === 'models' ? 'done' : 'todo' },
		{ label: 'Providers & models', icon: 'chip', state: page === 'models' ? 'current' : 'todo' },
	]);
	function withReportAction(row: AiRow): AiRow {
		return row.reportable ? { ...row, more: [...(row.more ?? []), { label: 'Save a report', run: () => void saveSetupReport() }] } : row;
	}
	const jobs = $derived<SetupJob[]>([
		...(page !== 'models' && (downloading || localFailed) ? [{ label: 'Local model', state: localFailed ? 'error' as const : 'busy' as const,
			meta: localFailed ? (localStartError ? startFix : local?.fix) ?? '' : local?.local_state === 'starting' ? 'Starting the local model…' : ['Downloading', `${local?.download_percent ?? 0}%`, measuredDownloadTime(local?.download_eta_seconds)].filter(Boolean).join(' · '), progress: (local?.download_percent ?? 0) / 100 }] : []),
		...(classifier.job ? [classifier.job] : []),
		...(accountBusy ? [{ label: 'Starting CORSIA', state: 'busy' as const }] : []),
	]);
	const aiRows = $derived.by((): AiRow[] => {
		const rows = ['claude-code', 'codex'].map((vendor): AiRow => {
			const source = sources.find((row) => row.source === vendor);
			if (!source) {
				const name = vendor === 'codex' ? 'Codex' : 'Claude Code';
				return sourceError ? { key: vendor, name, meta: `Couldn’t check ${name}.`, error: true, action: { label: 'Retry', run: () => void refreshSources(true) } } : { key: vendor, name, meta: '', busy: true };
			}
			const view = connectFlow.row(source);
			const works = catalog.some((row) => row.connection === vendor && setupModelWorks(row));
			return withReportAction({ key: vendor, name: vendor === 'codex' ? 'Codex' : 'Claude Code', ...view, pill: works ? { state: 'success', label: 'Ready' } : undefined });
		});
		rows.push(withReportAction(setupLocalRow({ source: local, pick: localPick.pick, eligible, downloading, ready: catalog.some((row) => row.connection === 'omlx' && setupModelWorks(row)), startError: localStartError, startFix, startReference: localStartReference, freeDisk: status.free_disk_gb,
			start: () => void startLocal(), stop: () => void stopLocal(), check: () => void checkLocal() })));
		const provider = tested.find((test) => bestSetupModel(catalog.filter((row) => row.connection !== 'omlx' && row.connection !== 'claude-code' && row.connection !== 'codex'), [test]));
		rows.push({ key: 'api', name: 'API provider', meta: provider ? `${provider.source} · passed a test call` : 'Pay as you go, with a key from Anthropic, OpenAI or another provider.',
			pill: provider ? { state: 'success', label: 'Ready' } : undefined, action: provider ? undefined : { label: 'Add provider', run: () => { showAdd = true; } } });
		return rows;
	});
	const finishNote = $derived(setupStartNote(canFinish, eligible, downloading, local?.download_eta_seconds));
	type View = { title: string; lede?: string; mark?: boolean; primary?: { label: string; disabled?: boolean; loading?: boolean; note?: string; noteAlways?: boolean }; onPrimary?: () => void };
	const view = $derived.by((): View => {
		if (page === 'connect') return remote.view;
		if (page === 'checking') return { title: 'Checking this computer' };
		if (page === 'error') return { title: 'CORSIA couldn’t start', lede: reportError || statusError || 'Something went wrong while starting. Try again, or save a report to send.', primary: { label: 'Retry' }, onPrimary: resume ? resumeSetup : readStatus };
		if (page === 'hard-stop') {
			if (status.macos_supported === false) return { title: 'Update macOS to open CORSIA', lede: 'CORSIA needs macOS 15 or later. Update in System Settings › General › Software Update, then open CORSIA again.' };
			return { title: 'Move CORSIA to Applications', lede: 'Drag CORSIA into your Applications folder, then open it from there. It can’t update itself from the disk image.' };
		}
		if (screen === 'welcome') return { title: WELCOME_TITLE, mark: true, primary: { label: 'Continue' }, onPrimary: () => { welcomeSeen = true; } };
		if (page === 'account') return { title: $isSetupWide ? WELCOME_TITLE : 'Create your account', lede: $isSetupWide ? WELCOME_ACCOUNT_LINE : undefined, mark: $isSetupWide, primary: { label: 'Continue', disabled: !accountValid }, onPrimary: toFolder };
		if (page === 'folder') return { title: 'Choose your work folder', lede: 'Your projects and files live here.', primary: { label: 'Continue', loading: accountBusy, disabled: accountBusy || folderChanging || folderNotice?.kind === 'unwritable' || folderNotice?.kind === 'picker_failed' }, onPrimary: createAccount };
		return { title: 'Providers & models', lede: canFinish ? 'Ready to start. You can connect more here or later in Settings › Providers.' : 'Connect at least one to start chatting.', primary: { label: 'Start using CORSIA', disabled: !canFinish || finishing, loading: finishing, note: finishNote, noteAlways: true }, onPrimary: finishSetup };
	});
	function validateConfirmation() { confirmationError = confirmation && password !== confirmation ? 'Passwords don’t match.' : ''; }
	function toFolder() { validateConfirmation(); if (accountValid) page = 'folder'; }
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
			if (status.macos_supported === false || status.app_location === 'disk_image') { page = 'hard-stop'; return; }
			page = 'account'; void classifier.start();
			if (eligible) { await localPick.start(); await startLocal(); await readLocalProgress(); }
		} catch { statusError = 'Something went wrong while starting. Retry, or save a report to send.'; page = 'error'; }
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
			if (result === 'consent_required') otherQuestion = true;
			else if (result === 'foreign_omlx_changed') { localStartError = 'other_omlx'; startFix = fix; }
			else if (result === 'failed') { localStartError = 'other'; startFix = fix; }
			if (current) await refreshSources();
		} catch { localStartError = 'other'; startFix = LOCAL_START_NETWORK_FAILURE; }
		finally { localBusy = false; }
	}
	async function chooseOther(stop: boolean) {
		localBusy = true; localStartError = ''; localStartReference = ''; startFix = undefined;
		try {
			const current = get(token);
			const { result, reference, fix } = await requestLocalStart(current, stop ? 'stop' : 'keep');
			localStartReference = reference ?? '';
			if (result === 'foreign_omlx_changed') { localStartError = 'other_omlx'; startFix = fix; return; }
			if (result !== 'started') { localStartError = 'other'; startFix = fix; return; }
			otherQuestion = false;
			if (current) await refreshSources();
		} catch { localStartError = 'other'; startFix = LOCAL_START_NETWORK_FAILURE; }
		finally { localBusy = false; }
	}
	async function stopLocal() {
		const current = get(token);
		if (current) { await cancelLocalDownload(current); await refreshSources(); }
		else await fetch('/api/setup/local-download/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
	}
	async function connectToAnotherComputer() {
		await stopLocal();
		remote.reset(); page = 'connect';
	}
	async function createAccount() {
		if (!accountValid || accountBusy) return;
		accountBusy = true; accountError = ''; accountUsernameError = '';
		try {
			if (folderChanged) { await applyWorkFolder(folder); folderChanged = false; }
			const response = await fetch('/api/setup/first-account', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim(), username: username.trim(), password, email: email.trim() || null }) });
			if (response.status === 409) { accountExists = true; accountError = 'This computer already has a CORSIA account. Sign in instead.'; page = 'account'; return; }
			const result = await response.json().catch(() => null);
			if (!response.ok) {
				if (response.status === 422) { page = 'account'; accountUsernameError = "That username isn't available. Choose another."; }
				else accountError = 'Couldn’t create your account. Retry.';
				return;
			}
			onAccountCreated(); token.set(result.token); user.set(result.user ?? { id: result.user_id, name: name.trim() }); connect();
			password = ''; confirmation = ''; page = 'models';
			await Promise.all([refreshSources(), classifier.read(), loadProviders()]);
		} catch { accountError = 'Couldn’t create your account. Retry.'; }
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
			if (!await recordSetupProgress({ completed: true, handoff: { tools: connectFlow.attempts } }, current)) throw new SetupFirstModelError('progress_save');
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
<SetupShell {steps} section={page === 'connect' ? 'Connect' : 'Setup'} {jobs} page={page === 'connect' ? `connect-${remote.stage}` : screen} title={view.title} lede={view.lede} primary={view.primary} mark={view.mark} aside={page === 'account' && $isSetupWide ? welcomeAside : undefined} onPrimary={view.onPrimary}>
	{#snippet secondary()}
		{#if page === 'connect'}<SetupConnectBack onclick={() => remote.stage === 'code' ? remote.toChoose() : leaveConnect()} />
		{:else if (screen === 'welcome' || page === 'account') && canConnect}<Button variant="chip" onclick={() => void connectToAnotherComputer()}>Connect to CORSIA on another computer</Button>
		{:else if page === 'folder'}<Button variant="chip" disabled={accountBusy} onclick={() => { page = 'account'; }}>Back</Button>{#if accountError}<Button variant="chip" onclick={async () => { accountError = await saveSetupReport(); }}>Save a report</Button>{/if}
		{:else if page === 'hard-stop'}<Button variant="chip" onclick={readStatus}>Check again</Button>
		{:else if page === 'error'}<Button variant="chip" onclick={async () => { reportError = await saveSetupReport(); }}>Save a report</Button>{/if}
	{/snippet}
	{#if page === 'connect'}<SetupConnect {remote} />
	{:else if screen === 'welcome'}<SetupWelcome />
	{:else if page === 'account'}
		<SetupAccountForm bind:name bind:username bind:password bind:confirmation bind:email {usernameError} {confirmationError} requiredUsername={status.required_username} {accountError} onSubmit={toFolder} onValidateConfirmation={validateConfirmation} />
		{#if accountExists}<Button variant="chip" onclick={onExistingAccount}>Sign in</Button>{/if}
	{:else if page === 'folder'}<SetupWorkFolder bind:folder bind:changed={folderChanged} bind:changing={folderChanging} bind:notice={folderNotice} error={accountError} />
	{:else if page === 'models'}
		<SetupAiRows rows={aiRows} />
		{#if otherQuestion}<div class="other-omlx"><h2>Stop other oMLX processes?</h2><p>CORSIA runs its own oMLX for the local model. Stopping other oMLX processes can free memory. Login items start again when you next sign in. Other processes stay stopped until you start them.</p><Button variant="chip" disabled={localBusy} onclick={() => void chooseOther(true)}>Stop other oMLX</Button><Button variant="chip" disabled={localBusy} onclick={() => void chooseOther(false)}>Keep them running</Button></div>{/if}
		{#if sourceError}<p class="setup-error">{sourceError}</p><Button variant="chip" onclick={() => void refreshSources(true)}>Retry</Button>{/if}
	{/if}
</SetupShell>
{#if showAdd}<AddProviderModal onClose={() => { showAdd = false; void refreshSources(); }} onSelectCloud={selectProvider} />{/if}
{#if openProvider}<ProviderSettingsModal provider={openProvider} initialTab="advanced" onClose={() => { openProvider = null; void refreshSources(); }} />{/if}

<style>
	.other-omlx { margin: var(--space-4) var(--space-4); color: var(--text-secondary); }
	h2 { font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-primary); }
	p { font-size: var(--text-footnote); line-height: var(--lh-normal); }
	.setup-error { color: var(--red-text); margin: var(--space-2) var(--space-4); }
</style>
