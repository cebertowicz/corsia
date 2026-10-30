<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { get } from 'svelte/store';
	import { Button } from '$lib/components/ui';
	import SetupShell, { type SetupJob, type SetupStep } from './SetupShell.svelte';
	import SetupAiRows, { type AiRow } from './SetupAiRows.svelte';
	import SetupSourceError from './SetupSourceError.svelte';
	import SetupAccountForm from './SetupAccountForm.svelte';
	import ConfirmDialog from './ConfirmDialog.svelte';
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
	import { hasReadySetupModel, setupModelWorks, setupCatalog, setupProviderName, type SetupModel } from '$lib/setupModels';
	import { fetchSetupProgress, recordSetupProgress } from '$lib/setupProgress';
	import { saveSetupReport, withReportAction, type SetupReportRow } from '$lib/setupReport';
	import { createSetupClassifier, type ClassifierState } from '$lib/setupClassifier.svelte';
	import { createSetupLocalPick } from '$lib/setupLocalPick.svelte';
	import { createSetupRemote } from '$lib/setupRemote.svelte';
	import { applyWorkFolder, shellInfo } from '$lib/remoteShell';
	import { accountRefusal, LOCAL_START_NETWORK_FAILURE, passwordConfirmationError, requestLocalStart } from '$lib/setupFormSupport';
	import { setupLocalRow, setupStartNote, type LocalRowState } from '$lib/setupProviderRows';
	import { measuredDownloadTime, showLocalOnlyGuidance } from '$lib/setupLocalPresentation';
	import { ACCOUNT_LEDE, WELCOME_TITLE } from '$lib/setupWelcome';
	import { createSetupWarm, finishSetupWarmHandoff } from '$lib/setupWarm.svelte';
	interface Props { onExistingAccount: () => void; onAccountCreated: () => void; onFinished: () => void; startConnect?: boolean; onLeaveConnect?: () => void; resume?: boolean }
	const { onExistingAccount, onAccountCreated, onFinished, startConnect = false, onLeaveConnect, resume = false }: Props = $props();
	type Page = 'checking' | 'welcome' | 'account' | 'subscriptions' | 'models' | 'hard-stop' | 'error' | 'connect';
	const CLI_VENDORS = ['claude-code', 'codex'] as const;
	type Status = { apple_silicon?: boolean; macos_supported?: boolean | null; free_disk_gb?: number; local_eligible?: boolean; local_started?: boolean; work_folder?: string; required_username?: string | null; classifier?: ClassifierState };
	const classifier = createSetupClassifier(), localPick = createSetupLocalPick(), remote = createSetupRemote();
	const warm = createSetupWarm();
	$effect(() => { warm.start(catalog, $token); });
	let page = $state<Page>(untrack(() => startConnect) ? 'connect' : 'checking');
	let shell: SetupShell | undefined = $state();
	let status = $state<Status>({}), statusError = $state(''), reportError = $state('');
	let name = $state(''), username = $state(''), password = $state(''), confirmation = $state(''), email = $state('');
	let folder = $state(''), defaultFolder = $state(''), defaultFolderPath = $state(''), folderApplyPath = $state(''), savedFolder = $state(false), folderChanged = $state(false), folderChanging = $state(false), folderNotice = $state<FolderNotice>(null);
	let accountBusy = $state(false), accountError = $state(''), accountExists = $state(false), confirmationError = $state('');
	// Set once setup itself made the account: Back to the account screen then edits it, and only what differs from these is saved.
	let accountKnown = $state(false), savedName = '', savedEmail = '';
	let sources = $state<ModelSource[]>([]), catalog = $state<SetupModel[]>([]), sourceError = $state(''), sourceRetry = $state<{ error: string; vendors: string[] } | null>(null);
	let localStartError = $state<'other' | ''>(''), localStartReference = $state(''), startFix = $state<string | undefined>(), localBusy = $state(false);
	// The pre-start (pick, then start) is under way, so the local row shows progress instead of a Download button.
	let localPreparing = $state(false);
	let finishing = $state(false), showAdd = $state(false), openProvider = $state<HostProvider | null>(null);
	// The subscriptions answer; null until answered (or skipped, or a resumed setup), when every CLI row shows.
	// toolsOff: tools turned off from a row before the question was answered; nothing is recorded for them.
	let picks = $state<SetupPicks | null>(null), toolsOff = $state<string[]>([]), confirmLocalStop = $state(false), confirmConnect = $state(false), localOffFailed = $state<{ reference?: string } | undefined>();
	const connectFlow = createSetupConnect({ token: () => get(token), sources: () => sources, refreshSources });
	const canConnect = shellInfo() !== null;
	let preauthLocal = $state<LocalRowState | undefined>();
	const local = $derived(($token ? sources.find((source) => source.source === 'omlx') : preauthLocal) as LocalRowState | undefined);
	const eligible = $derived(status.local_eligible !== false);
	// On a server that is not macOS, setup lists no Local model row or memory reason.
	const neverLocal = $derived(local?.never_local === true || status.macos_supported === null);
	const downloading = $derived(['downloading', 'paused', 'starting'].includes(local?.local_state ?? ''));
	const localFailed = $derived(['failed', 'setup_failed', 'start_failed'].includes(local?.local_state ?? '') || Boolean(localStartError));
	let addedEndpoints = $state<string[]>([]); const tested = $derived($testedProviders.map((source) => ({ source })));
	const canFinish = $derived(hasReadySetupModel(catalog, tested, addedEndpoints));
	const cliSignedIn = (vendor: string) => { const source = sources.find((row) => row.source === vendor); return Boolean(source && (sourceIsReady(source) || source.limited)); };
	const cliPicked = (vendor: string) => !toolsOff.includes(vendor) && (picks === null || (vendor === 'codex' ? picks.chatgpt : picks.claude));
	const subscriptionsSkipped = $derived(subscriptionQuestion(cliSignedIn('claude-code'), cliSignedIn('codex')) === null);
	// A working cloud model frees the local download to be turned off.
	const cloudReady = $derived(catalog.some((row) => row.connection !== 'omlx' && setupModelWorks(row)));
	const localReady = $derived(catalog.some((row) => row.connection === 'omlx' && setupModelWorks(row)));
	const localModelActive = $derived(downloading || localPreparing || localBusy || localPick.pick?.state === 'checking' || localReady);
	const localOnly = $derived(showLocalOnlyGuidance({ eligible, localModelActive, cloudReady }));
	const usernameError = $derived(username && !/^[a-z0-9_-]{1,32}$/.test(username) ? 'Use up to 32 lowercase letters, numbers, dashes or underscores.' : '');
	const accountName = $derived(username.trim() || $user?.name || '');
	const editing = $derived(Boolean($token) && accountKnown);
	const folderDecisionPending = $derived(folderNotice !== null);
	// A new account needs every field; an existing one needs a name, and a password only when one is being changed.
	const canSubmitAccount = $derived(!folderDecisionPending && (editing ? Boolean(name.trim() && (!password || (password.length >= 8 && confirmation === password))) : Boolean(name.trim() && username.trim() && !usernameError && password.length >= 8 && confirmation === password)));
	const steps = $derived<SetupStep[]>(page === 'connect' ? remote.steps : [
		{ label: 'Getting started', icon: 'computer', state: ['checking', 'welcome', 'hard-stop'].includes(page) || (page === 'error' && !$token) ? 'current' : 'done' },
		{ label: 'Account & workspace', icon: 'person', state: page === 'account' ? 'current' : ['subscriptions', 'models'].includes(page) || (page === 'error' && $token) ? 'done' : 'todo' },
		{ label: 'Your subscriptions', icon: 'card', state: page === 'subscriptions' ? 'current' : page === 'models' ? 'done' : 'todo' },
		{ label: 'Providers & models', icon: 'chip', state: page === 'models' ? 'current' : 'todo' },
	]);
	/** Every failed row's cause and reference, so the saved report says what went wrong where. */
	function reportRows(): SetupReportRow[] {
		return aiRows.filter((row) => row.error).map((row) => ({
			key: row.key as SetupReportRow['key'], name: row.name, reference: row.reference,
			cause: row.key === 'omlx' ? (localOffFailed ? 'turn_off_failed' : local?.setup_error_code ?? (localStartError || undefined)) : connectFlow.attempts[row.key]?.cause,
		}));
	}
	const jobs = $derived<SetupJob[]>([
		...(page !== 'models' && (downloading || localFailed) ? [{ label: 'Local model', state: localFailed ? 'error' as const : 'busy' as const,
			meta: localFailed ? (localStartError ? startFix : local?.fix) ?? '' : local?.local_state === 'starting' ? 'Starting the local model…' : ['Downloading', `${local?.download_percent ?? 0}%`, measuredDownloadTime(local?.download_eta_seconds)].filter(Boolean).join(' · '), progress: (local?.download_percent ?? 0) / 100 }] : []),
		...(accountBusy ? [{ label: 'Starting CORSIA', state: 'busy' as const }] : []),
	]);
	const aiRows = $derived.by((): AiRow[] => {
		const rows = CLI_VENDORS.filter((vendor) => cliSignedIn(vendor) || cliPicked(vendor)).map((vendor): AiRow => {
			const source = sources.find((row) => row.source === vendor);
			if (sourceRetry?.vendors.includes(vendor)) return { key: vendor, name: vendor === 'codex' ? 'Codex' : 'Claude Code', meta: '', busy: true };
			if (!source || (sourceError && connectFlow.awaitingSource(vendor))) {
				const name = vendor === 'codex' ? 'Codex' : 'Claude Code';
				return sourceError ? { key: vendor, name, meta: `Couldn’t check ${name}.`, error: true } : { key: vendor, name, meta: '', busy: true };
			}
			const view = connectFlow.row(source);
			const works = catalog.some((row) => row.connection === vendor && setupModelWorks(row));
			const toggle = view.canTurnOff ? { on: true, run: (next: boolean) => { if (!next) turnOffTool(vendor); } } : undefined;
			return withReportAction({ key: vendor, name: vendor === 'codex' ? 'Codex' : 'Claude Code', ...view, toggle, pill: works ? { state: 'success', label: 'Ready' } : undefined }, reportRows);
		});
		if (!neverLocal) rows.push(withReportAction({ ...setupLocalRow({ source: local, pick: localPick.pick, eligible, downloading, preparing: localPreparing || localBusy || localPick.pick?.state === 'checking', ready: localReady, startError: localStartError, startFix, startReference: localStartReference, freeDisk: status.free_disk_gb,
			cloudReady, confirmingStop: confirmLocalStop, turnOffFailed: localOffFailed,
			start: () => void startLocal(), stop: () => void stopLocal(), check: () => void checkLocal(),
			confirmStop: (next) => { confirmLocalStop = next; }, turnOff: () => void turnOffLocal() }), localOnly }, reportRows));
		const provider = tested.find((test) => hasReadySetupModel(catalog.filter((row) => row.connection !== 'omlx' && row.connection !== 'claude-code' && row.connection !== 'codex'), [test]));
		const endpoint = addedEndpoints.find((id) => hasReadySetupModel(catalog.filter((row) => row.connection === 'external'), [], [id])), ready = provider || endpoint;
		rows.push({ key: 'api', name: 'API provider', meta: provider ? `${setupProviderName(provider.source)} · passed a test call` : endpoint ? `${setupProviderName(endpoint)} · added` : 'Pay as you go, with a key from Anthropic, OpenAI or another provider.', pill: ready ? { state: 'success', label: 'Ready' } : undefined, action: ready ? undefined : { label: 'Add provider', run: () => { showAdd = true; } } });
		return rows;
	});
	const finishNote = $derived(setupStartNote(canFinish, eligible, downloading || localPreparing, local?.download_eta_seconds));
	type View = { title: string; lede?: string; primary?: { label: string; disabled?: boolean; loading?: boolean; note?: string; noteAlways?: boolean }; onPrimary?: () => void };
	const view = $derived.by((): View => {
		if (page === 'connect') return remote.view;
		if (page === 'checking') return { title: 'Checking this computer' };
		if (page === 'error') return { title: 'CORSIA couldn’t start', lede: reportError || statusError || 'Something went wrong while starting. Try again, or save a report to send.', primary: { label: 'Retry' }, onPrimary: resume ? resumeSetup : readStatus };
		if (page === 'hard-stop') return { title: 'Update macOS to open CORSIA', lede: 'CORSIA needs macOS 15 or later. Update in System Settings › General › Software Update, then open CORSIA again.' };
		if (page === 'welcome') return { title: WELCOME_TITLE, primary: { label: 'Continue' }, onPrimary: () => { page = 'account'; } };
		// A resumed setup has no account details to show, so it reads back what is set.
		if (page === 'account' && $token && !accountKnown) return { title: 'Your account', primary: { label: 'Continue' }, onPrimary: afterAccount };
		if (page === 'account') return { title: editing ? 'Your account' : 'Create your account', lede: ACCOUNT_LEDE, primary: { label: 'Continue', loading: accountBusy, disabled: !canSubmitAccount || accountBusy || folderChanging }, onPrimary: submitAccount };
		if (page === 'subscriptions') return { title: 'Your subscriptions', lede: 'CORSIA’s agents can work through a Claude or ChatGPT subscription you already pay for.' };
		if (page === 'models' && finishing && warm.running) return { title: 'Finishing setup', lede: warm.percent === null ? undefined : `${warm.percent}%` };
		return { title: 'Providers & models', lede: 'CORSIA needs one working model to continue setup.', primary: { label: 'Start using CORSIA', disabled: !canFinish || finishing, loading: finishing, note: finishNote, noteAlways: true }, onPrimary: finishSetup };
	});
	/** Continue on the account screen creates the account, applying the chosen folder first, or saves what changed on one setup already made. */
	async function submitAccount() {
		confirmationError = passwordConfirmationError(password, confirmation);
		if (!canSubmitAccount) return;
		if (editing) await updateAccount(); else if (!get(token)) await createAccount();
	}
	/** The subscriptions screen is asked only while a CLI is still not signed in. */
	function afterAccount() { page = subscriptionsSkipped ? 'models' : 'subscriptions'; }
	/** One answer: picked tools start installing now, an unpicked one stops what it started. */
	function recordPicks(next: SetupPicks) {
		picks = next; toolsOff = [];
		// Kept so the first chat knows which subscriptions the person said they pay for.
		void recordSetupProgress({ picks: next }, get(token));
	}
	/** The row's switch: the tool is unpicked as on the subscriptions screen, and what it started stops. */
	function turnOffTool(vendor: string) {
		// With no answer recorded, the other tool's pick is unknown: it is never written as a yes the person didn't give.
		if (picks) recordPicks({ claude: vendor === 'claude-code' ? false : picks.claude, chatgpt: vendor === 'codex' ? false : picks.chatgpt });
		else toolsOff = [...toolsOff, vendor];
		void connectFlow.stop(vendor);
	}
	function answerSubscriptions(next: SetupPicks) {
		recordPicks(next);
		for (const vendor of CLI_VENDORS) {
			if (cliSignedIn(vendor)) continue;
			if (cliPicked(vendor)) connectFlow.pick(vendor);
			else void connectFlow.stop(vendor);
		}
		page = 'models';
	}
	function leaveConnect() { if (startConnect) onLeaveConnect?.(); else page = 'welcome'; }
	function setFolderState(next: Status) {
		const info = shellInfo();
		const workspace = info?.mode === 'local' ? info.default_workspace : undefined;
		if (workspace) { defaultFolder = workspace.display; defaultFolderPath = workspace.path; }
		if (next.work_folder) {
			folder = next.work_folder;
			// Status names the current display folder even for the initial shell-owned default.
			savedFolder = workspace ? next.work_folder !== workspace.display : true;
		} else {
			savedFolder = false;
			if (workspace) folder = workspace.display;
		}
		folderApplyPath = '';
	}
	async function readStatus() {
		page = 'checking'; statusError = '';
		try {
			const response = await fetch('/api/setup/status');
			if (response.status === 409) { onExistingAccount(); return; }
			if (!response.ok) throw new Error('status');
			status = await response.json(); setFolderState(status);
			if (status.required_username) username = status.required_username;
			if (status.classifier) classifier.accept(status.classifier);
			if (status.macos_supported === false) { page = 'hard-stop'; return; }
			page = 'welcome'; void classifier.start();
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
		if (!canSubmitAccount || accountBusy) return;
		accountBusy = true; accountError = '';
		try {
			if (folderChanged) {
				if (!folderApplyPath) throw new Error('work folder path is unavailable');
				await applyWorkFolder(folderApplyPath); folderChanged = false;
			}
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
			accountKnown = true; savedName = name.trim(); savedEmail = email.trim();
			await Promise.all([refreshSources(), classifier.read(), loadProviders()]);
			afterAccount();
		} catch { accountError = 'Couldn’t create your account. Try again.'; }
		finally { accountBusy = false; }
	}
	/** Back on the account screen: writes only the fields that differ from what setup saved, and nothing at all when none do. */
	async function updateAccount() {
		if (accountBusy) return;
		const changes: { name?: string; email?: string; password?: string } = {};
		if (name.trim() !== savedName) changes.name = name.trim();
		if (email.trim() !== savedEmail) changes.email = email.trim();
		if (password) changes.password = password;
		if (Object.keys(changes).length === 0) { afterAccount(); return; }
		accountBusy = true; accountError = '';
		try {
			const send = () => fetch('/api/setup/account', { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${get(token)}` }, body: JSON.stringify(changes) });
			let response = await send();
			let result = await response.json().catch(() => null);
			// An email has nowhere to go until the profile exists, and the name on screen is what builds it.
			if (response.status === 422 && result?.code === 'profile_missing' && changes.name === undefined) {
				changes.name = name.trim();
				response = await send();
				result = await response.json().catch(() => null);
			}
			// Setup finished elsewhere: the account step is over, and trying again would only loop.
			if (response.status === 409) { onFinished(); return; }
			if (!response.ok) {
				const reference = typeof result?.ref === 'string' && result.ref ? ` Reference: ${result.ref}` : '';
				accountError = (response.status === 422 ? accountRefusal(result?.detail) : null) ?? `Couldn’t save your account. Try again.${reference}`;
				return;
			}
			// A new password ends the old session, so the reply carries the next one.
			if (result?.token) { token.set(result.token); user.set(result.user ?? get(user)); connect(); }
			savedName = name.trim(); savedEmail = email.trim(); password = ''; confirmation = '';
			afterAccount();
		} catch { accountError = 'Couldn’t save your account. Try again.'; }
		finally { accountBusy = false; }
	}
	/** A failed strict check (GitHub, an endpoint) still leaves the tools it did check readable, so a picked install can start. */
	async function refreshSources(strict = false) {
		const current = get(token); if (!current) return;
		try {
			sources = await (strict ? recheckSources(current) : fetchSources(current));
			connectFlow.sourcesChanged();
			catalog = await setupCatalog(current);
			sourceError = '';
		} catch { catalog = []; sourceError = 'CORSIA couldn’t check your model sources. Try again.'; if (strict) await fetchSources(current).then((next) => { sources = next; connectFlow.sourcesChanged(); }, () => undefined); }
	}
	async function retrySources() {
		sourceRetry = { error: sourceError, vendors: CLI_VENDORS.filter((vendor) => !sources.some((row) => row.source === vendor) || connectFlow.awaitingSource(vendor)) };
		try { await refreshSources(true); } finally { sourceRetry = null; }
	}
	/** Setup never chooses a model: the person picks the CORSIA agent's on its first chat. */
	function finishSetup() {
		return finishSetupWarmHandoff({
			session: get(token), isFinishing: finishing, setFinishing: (next) => { finishing = next; }, setSourceError: (next) => { sourceError = next; },
			recordProgress: (current) => recordSetupProgress({ completed: true, handoff: { tools: connectFlow.attempts }, ...(picks ? { picks } : {}) }, current),
			getCatalog: () => catalog, warm, exitMark: () => shell?.exitMark(), onFinished: () => onFinished(),
		});
	}
	async function resumeSetup() {
		const progress = await fetchSetupProgress(get(token));
		if (!progress) { page = 'error'; return; }
		if (progress.setup_completed) { onFinished(); return; }
		status = (progress.status ?? {}) as Status; setFolderState(status);
		page = 'models';
		if (status.classifier) classifier.accept(status.classifier);
		// A relaunch after the account step has no screen-1 start to rerun, so a local download that was never claimed starts here.
		const unstarted = eligible && status.local_started === false;
		if (unstarted) localPreparing = true;
		try {
			await Promise.all([refreshSources(true), classifier.read(), loadProviders()]);
			if (unstarted) await startLocal();
		} finally { localPreparing = false; }
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
		const focus = () => { if (page === 'models') void refreshSources(true); void classifier.retryIfFailed(); };
		window.addEventListener('focus', focus);
		return () => { warm.destroy(); offModels(); offClassifier(); window.removeEventListener('focus', focus); };
	});
</script>
<SetupShell bind:this={shell} {steps} section={page === 'connect' ? 'Connect' : 'Setup'} {jobs} page={page === 'connect' ? `connect-${remote.stage}` : page} title={view.title} lede={view.lede} primary={view.primary} onPrimary={view.onPrimary}>
	{#snippet secondary()}
		{#if page === 'connect'}<SetupConnectBack onclick={() => remote.stage === 'code' ? remote.toChoose() : leaveConnect()} />
		{:else if page === 'welcome' && canConnect && !$token}<Button variant="chip" onclick={() => downloading ? (confirmConnect = true) : void connectToAnotherComputer()}>Connect to CORSIA on another computer</Button>
		{:else if page === 'account'}<Button variant="chip" disabled={accountBusy} onclick={() => { page = 'welcome'; }}>Back</Button>{#if accountError}<Button variant="chip" onclick={async () => { accountError = await saveSetupReport(); }}>Save a report</Button>{/if}
		{:else if page === 'subscriptions'}<Button variant="chip" onclick={() => { page = 'account'; }}>Back</Button>
		{:else if page === 'models'}<Button variant="chip" disabled={finishing} onclick={() => { page = subscriptionsSkipped ? 'account' : 'subscriptions'; }}>Back</Button>
		{:else if page === 'hard-stop'}<Button variant="chip" onclick={readStatus}>Check again</Button>
		{:else if page === 'error'}<Button variant="chip" onclick={async () => { reportError = await saveSetupReport(); }}>Save a report</Button>{/if}
	{/snippet}
	{#if page === 'connect'}<SetupConnect {remote} />
	{:else if page === 'welcome'}<SetupWelcome />
	{:else if page === 'account' && $token && !accountKnown}
		<p class="settled" data-testid="setup-account-made">{accountName ? `Your account is set up as ${accountName}.` : 'Your account is set up.'}</p>
		<p class="settled" data-testid="setup-folder-made">CORSIA keeps your work in {folder}.</p>
	{:else if page === 'account'}
		<SetupAccountForm bind:name bind:username bind:password bind:confirmation bind:email {usernameError} {confirmationError} requiredUsername={status.required_username} {editing} {accountError} onSubmit={submitAccount} onValidateConfirmation={() => confirmationError = passwordConfirmationError(password, confirmation)} />
		<!-- Creating the account scaffolds the work folder, so after it the folder is read back, not offered. -->
		{#if editing}<p class="settled" data-testid="setup-folder-made">CORSIA keeps your work in {folder}.</p>
		{:else}<SetupWorkFolder bind:folder {defaultFolder} {defaultFolderPath} bind:applyPath={folderApplyPath} {savedFolder} bind:changed={folderChanged} bind:changing={folderChanging} bind:notice={folderNotice} />{/if}
		{#if accountExists}<Button variant="chip" onclick={onExistingAccount}>Sign in</Button>{/if}
	{:else if page === 'subscriptions'}<SetupSubscriptions claudeReady={cliSignedIn('claude-code')} codexReady={cliSignedIn('codex')} {eligible} answer={picks} onAnswer={answerSubscriptions} />
	{:else if page === 'models'}
		<SetupAiRows rows={aiRows} disableRetry={sourceRetry !== null} />
		<SetupSourceError error={sourceRetry?.error ?? sourceError} retrying={sourceRetry !== null} retry={retrySources} />
	{/if}
</SetupShell>
<ConfirmDialog open={confirmConnect} title="Connect to another computer instead?" description="This stops the download on this computer." confirmLabel="Stop and connect" cancelLabel="Keep downloading" onConfirm={() => { confirmConnect = false; void connectToAnotherComputer(); }} onCancel={() => { confirmConnect = false; }} />
{#if showAdd}<AddProviderModal onClose={() => { showAdd = false; void refreshSources(); }} onSelectCloud={selectProvider} onAddedEndpoint={(id) => { addedEndpoints = [...new Set([...addedEndpoints, id])]; }} />{/if}
{#if openProvider}<ProviderSettingsModal provider={openProvider} initialTab="advanced" onClose={() => { openProvider = null; void refreshSources(); }} />{/if}
<style>
	p { font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.settled { margin: 0; font-size: var(--text-title3); color: var(--text-primary); }
</style>
