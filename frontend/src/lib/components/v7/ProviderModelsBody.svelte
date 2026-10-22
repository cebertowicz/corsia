<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { get } from 'svelte/store';
	import { localDownloadMeta, localDownloadRows, type LocalDownloadRows } from '$lib/stores/localModelDownloadRows';
	import {
		providerModels,
		loadProviderModelsFor,
		pollJobsFor,
		modelsForProvider,
		markModelOptimistic,
		type ProviderModel,
	} from '$lib/stores/providerModels';
	import { loadModel, unloadModel, cancelJob } from '$lib/stores/providerModelCommands';
	import { loadModels, workspaceModels as catalogModels } from '$lib/stores/models';
	import { chatPickerWarning } from './modelWarning';
	import { omlxAdminUrl } from '$lib/stores/providerModelUtils';
	import {
		canLoadAnyway as canLoadAnywayFor, checkLoad, createModelsPoller, keepLine as keepLineFor, putPinned, type PreloadInfo,
	} from '$lib/stores/providerModelsHost';
	import { on } from '$lib/ws/events';
	import type { HostProvider } from '$lib/stores/providersV7';
	import ModelRow from './ModelRow.svelte';
	import PreLoadCheckModal from './PreLoadCheckModal.svelte';
	import ModelDetailsModal from './ModelDetailsModal.svelte';
	import ProviderModelsEmpty from './ProviderModelsEmpty.svelte';
	import LocalSettingsDownloadStatus from './LocalSettingsDownloadStatus.svelte';
	import ProviderLocalModelControls from './ProviderLocalModelControls.svelte';
	import ProviderModelsAvailableControls from './ProviderModelsAvailableControls.svelte';
	import LocalModelDeleteDownloadModal from './LocalModelDeleteDownloadModal.svelte';
	import ProviderModelsActionError from './ProviderModelsActionError.svelte';
	import ProviderModelsPhase from './ProviderModelsPhase.svelte';
	interface Props {
		provider: HostProvider;
		isLocal?: boolean;
		onAddProvider?: () => void;
	}

	const { provider, isLocal = false, onAddProvider = () => {} }: Props = $props();

	let preloadInfo = $state<PreloadInfo | null>(null);
	let detailsFor = $state<ProviderModel | null>(null);
	let deleteCandidate = $state<ProviderModel | null>(null);

	let stopLocalUpdates: (() => void) | null = null;
	let showAllAvailable = $state(false);
	let actionError = $state('');
	let hasLocalDownload = $state(false);
	let localRows = $state<LocalDownloadRows>({});

	const pmState = $derived(modelsForProvider($providerModels, provider.raw, provider.server));
	const models = $derived(pmState.models);

	const grouped = $derived.by(() => {
		const loaded: ProviderModel[] = [];
		const loading: ProviderModel[] = [];
		const available: ProviderModel[] = [];
		for (const m of models) {
			if (m.state === 'loaded') loaded.push(m);
			else if (m.starting) loading.push({ ...m, state: 'loading' }); // oMLX is loading its pins at start
			else if (m.state === 'loading' || m.state === 'cache_warming' || m.state === 'queued') loading.push(m);
			else available.push(m);
		}
		return { loaded, loading, available };
	});

	const isHarness = $derived(provider.type === 'harness');
	const availableInitialShow = 5;
	const visibleAvailable = $derived(
		showAllAvailable ? grouped.available : grouped.available.slice(0, availableInitialShow),
	);
	const hiddenAvailableCount = $derived(grouped.available.length - visibleAvailable.length);

	// A registered endpoint, cloud API or CLI runs its own models: nothing loads, a model is on or off.
	const isExternal = $derived(provider.server?.kind === 'external');
	const isCorsiaOmlx = $derived(provider.name === 'omlx' && provider.server?.corsia_omlx === true); // CORSIA's managed oMLX only
	const adminUrl = $derived(provider.name === 'omlx' ? omlxAdminUrl(provider.server?.mgmt_base ?? undefined) : undefined);
	const catalogOnly = $derived(provider.type === 'cloud' || isExternal || isHarness);
	// A harness section names the switch, not picker delivery; the row warning says when the picker can't offer it.
	const loadedLabel = $derived(isHarness ? 'Shown' : catalogOnly ? 'Active' : 'Loaded');
	const availableLabel = $derived(isHarness ? 'Hidden' : 'Available');
	// The switch's name says what it does here: nothing loads unless a local engine runs the model.
	const toggleLabels = $derived<[string, string]>(
		isHarness ? ['Hide from picker', 'Show in picker'] : catalogOnly ? ['Turn off', 'Turn on'] : ['Unload', 'Load'],
	);
	// The switch on a row of CORSIA's own oMLX means "keep this loaded": on while loaded or kept.
	// A person's oMLX, here or on another computer, keeps Load and Unload: CORSIA never pins it.
	const keepsLoaded = $derived(isCorsiaOmlx && !catalogOnly);
	const keepLine = (m: ProviderModel) => (keepsLoaded ? keepLineFor(m, localRows.finishing === m.id) : null);
	const canLoadAnyway = (m: ProviderModel) => keepsLoaded && canLoadAnywayFor(m, localRows.finishing === m.id);
	async function setPinned(m: ProviderModel, pinned: boolean): Promise<boolean> {
		const t = get(token);
		return !!t && !!provider.server?.id && putPinned(t, provider.server.id, m.id, pinned);
	}
	const poller = createModelsPoller(async () => {
		await pollJobsFor(provider.raw, provider.server);
		// oMLX's start preload has no job: read the list again until it ends.
		if (models.some((m) => m.starting)) await loadProviderModelsFor(provider.raw, provider.server);
		return models.some((m) => m.starting || m.state === 'loading' || m.state === 'cache_warming' || m.state === 'queued');
	});
	const _ensurePolling = poller.ensure;
	async function refreshLocalDownloads() {
		if (provider.name !== 'omlx' || !provider.server?.id) return;
		const t = get(token);
		if (!t) return;
		const rows = await localDownloadRows(t, provider.server.id);
		if (rows.active !== undefined) hasLocalDownload = rows.active;
		localRows = { ...localRows, ...rows };
	}

	onMount(() => {
		void Promise.all([loadProviderModelsFor(provider.raw, provider.server), refreshLocalDownloads()]).then(() => {
			if (!catalogOnly) {
				void pollJobsFor(provider.raw, provider.server);
				_ensurePolling();
			}
		});
		if (provider.name === 'omlx') {
			stopLocalUpdates = on('models_updated', () => {
				void Promise.all([loadProviderModelsFor(provider.raw, provider.server), refreshLocalDownloads()]);
			});
		}
	});

	onDestroy(() => {
		poller.stop();
		stopLocalUpdates?.();
		// The extracted Settings download status cleans up its own subscription.
	});

	// A saved checkpoint window can make a row usable; show the new window at once.
	async function _windowChanged() {
		await loadProviderModelsFor(provider.raw, provider.server);
		loadModels();
		if (detailsFor) detailsFor = models.find((m) => m.id === detailsFor?.id) ?? detailsFor;
	}
	async function _doLoad(m: ProviderModel): Promise<boolean> {
		const r = await loadModel(provider.raw, provider.server, m.id, m.dbId);
		if (!r.ok) {
			actionError = r.message ?? 'Failed';
			return false;
		}
		await loadProviderModelsFor(provider.raw, provider.server);
		loadModels();
		return true;
	}
	async function _doUnload(m: ProviderModel) {
		const r = await unloadModel(provider.raw, provider.server, m.id, m.dbId);
		if (!r.ok) {
			actionError = r.message ?? 'Failed';
			return;
		}
		await loadProviderModelsFor(provider.raw, provider.server);
		loadModels();
	}
	/** Loads the model; false when it did not load, including when the person cancels the memory check. */
	async function _checkAndLoad(m: ProviderModel): Promise<boolean> {
		const estimatedGb = m.sizeBytes ? m.sizeBytes / (1024 ** 3) : 0;
		if (!isLocal || estimatedGb <= 0) return _doLoad(m);
		const data = await checkLoad(get(token) || '', estimatedGb, provider.name);
		if (!data || data.ok || data.remote) return _doLoad(m);
		return new Promise<boolean>((resolve) => { preloadInfo = {
			modelName: m.name,
			estimatedGb,
			freeGb: data.free_gb ?? 0,
			afterLoadGb: data.after_load_gb ?? 0,
			safeReserveGb: data.safe_reserve_gb ?? 0,
			warning: data.warning ?? 'Loading may exceed safe memory.',
			onConfirm: async () => { preloadInfo = null; resolve(await _doLoad(m)); },
			onCancel: () => { preloadInfo = null; resolve(false); },
		}; });
	}
	// Turning a local model on loads it, then keeps it loaded (pins) when the pins fit; cancelling the memory check changes nothing.
	async function turnOnLocal(m: ProviderModel) {
		markModelOptimistic(provider.raw, provider.server, m.id, 'loading');
		_ensurePolling();
		const loaded = await _checkAndLoad(m);
		if (!loaded) markModelOptimistic(provider.raw, provider.server, m.id, 'default');
		if (loaded && m.pinFits && !(await setPinned(m, true))) actionError = "Couldn’t keep this model loaded. Try again.";
		await loadProviderModelsFor(provider.raw, provider.server);
		loadModels();
	}
	async function turnOffLocal(m: ProviderModel) {
		if (m.pinned && !(await setPinned(m, false))) actionError = "Couldn’t turn this model off. Try again.";
		else if (m.state === 'loaded') {
			markModelOptimistic(provider.raw, provider.server, m.id, 'default');
			await _doUnload(m);
		}
		await loadProviderModelsFor(provider.raw, provider.server);
	}
	async function handleLoadAnyway(m: ProviderModel) {
		actionError = '';
		await _checkAndLoad(m);
		await loadProviderModelsFor(provider.raw, provider.server);
	}
	async function handleToggleModel(m: ProviderModel) {
		actionError = '';
		if (keepsLoaded) {
			if (m.state === 'loaded' || m.pinned) await turnOffLocal(m);
			else await turnOnLocal(m);
			return;
		}
		if (m.state === 'loaded') {
			markModelOptimistic(provider.raw, provider.server, m.id, 'default');
			await _doUnload(m);
		} else {
			const nextState = catalogOnly ? 'loaded' : 'loading';
			markModelOptimistic(provider.raw, provider.server, m.id, nextState);
			if (nextState === 'loading') _ensurePolling();
			await _checkAndLoad(m);
		}
	}
	async function handleCancel(m: ProviderModel) {
		actionError = '';
		if (!m.jobId) {
			markModelOptimistic(provider.raw, provider.server, m.id, 'default');
			return;
		}
		markModelOptimistic(provider.raw, provider.server, m.id, 'default');
		const r = await cancelJob(provider.server, m.jobId);
		if (!r.ok) actionError = r.message ?? 'Cancel failed';
		await loadProviderModelsFor(provider.raw, provider.server);
	}
	async function handleRetry(m: ProviderModel) {
		actionError = '';
		await _checkAndLoad(m);
	}
	function requestDownloadDeletion(model: ProviderModel) {
		deleteCandidate = model;
	}
	async function refreshAfterDownloadDeletion() {
		await loadProviderModelsFor(provider.raw, provider.server);
		loadModels();
	}
</script>

{#if preloadInfo}
	<PreLoadCheckModal
		modelName={preloadInfo.modelName}
		estimatedGb={preloadInfo.estimatedGb}
		freeGb={preloadInfo.freeGb}
		afterLoadGb={preloadInfo.afterLoadGb}
		safeReserveGb={preloadInfo.safeReserveGb}
		warning={preloadInfo.warning}
		onConfirm={preloadInfo.onConfirm}
		onCancel={preloadInfo.onCancel}
	/>
{/if}

{#if detailsFor}
	<ModelDetailsModal
		model={detailsFor}
		providerLabel={provider.label}
		providerName={provider.name}
		serverId={provider.server?.id}
		external={isExternal}
		labelEditable={isHarness}
		omlxAdminUrl={adminUrl}
		onChanged={_windowChanged}
		onClose={() => detailsFor = null}
	/>
{/if}

{#if deleteCandidate}
	<LocalModelDeleteDownloadModal
		model={deleteCandidate}
		onClose={() => deleteCandidate = null}
		onDeleted={refreshAfterDownloadDeletion}
	/>
{/if}

{#if pmState.loading && models.length === 0}
	<ProviderModelsPhase message="Loading models…" />
{:else if pmState.lastError}
	<ProviderModelsPhase message={`Failed to load models: ${pmState.lastError}`} error />
{:else if models.length === 0 && !hasLocalDownload}
	<ProviderModelsEmpty {provider} {isExternal} {isHarness} />
	{#if isCorsiaOmlx}
		<ProviderLocalModelControls onStarted={() => { void loadProviderModelsFor(provider.raw, provider.server); void refreshLocalDownloads(); loadModels(); }} {onAddProvider} corsiaManaged empty />
	{/if}
{:else}
	{#if actionError}
		<ProviderModelsActionError message={actionError} />
	{/if}

	{#if grouped.loaded.length > 0}
		<div class="section">{loadedLabel}</div>
		{#each grouped.loaded as m (m.id)}
			<ModelRow
				name={m.name}
				state={m.state}
				meta={localDownloadMeta(m.meta, localRows.speeds?.get(m.id))}
				caps={m.caps}
				warning={chatPickerWarning($catalogModels, m.dbId, isExternal) ?? keepLine(m)}
				toggleOn={true}
				{toggleLabels}
				onToggle={() => handleToggleModel(m)}
				onInfo={() => detailsFor = m}
			/>
		{/each}
	{/if}

	{#if grouped.loading.length > 0}
		<div class="section">Loading</div>
		{#each grouped.loading as m (m.id)}
			<ModelRow
				name={m.name}
				state={m.state}
				meta={localDownloadMeta(m.meta, localRows.speeds?.get(m.id))}
				caps={m.caps}
				progress={m.progress}
				loadingOn={keepsLoaded}
				{toggleLabels}
				onCancel={m.starting ? undefined : () => handleCancel(m)}
			/>
		{/each}
	{/if}

	{#if grouped.available.length > 0 || (provider.name === 'omlx' && hasLocalDownload)}
		<div class="section">{availableLabel}</div>
		{#if provider.name === 'omlx'}
			<LocalSettingsDownloadStatus onStatusChange={(hasDownload) => hasLocalDownload = hasDownload} />
		{/if}
		{#each visibleAvailable as m (m.id)}
			{@const speed = localRows.speeds?.get(m.id)}
			<ModelRow
				name={m.name}
				state={m.state}
				meta={localDownloadMeta(m.meta, speed)}
				caps={m.caps}
				note={speed !== undefined ? 'Settings measured on Macs like this one · omlx.ai' : null}
				warning={keepLine(m)}
				toggleOn={keepsLoaded && !!m.pinned}
				onLoadAnyway={canLoadAnyway(m) ? () => handleLoadAnyway(m) : undefined}
				{toggleLabels}
				onToggle={() => handleToggleModel(m)}
				onInfo={() => detailsFor = m}
				onRetry={m.state === 'error' ? () => handleRetry(m) : undefined}
				onDeleteDownload={isCorsiaOmlx ? () => requestDownloadDeletion(m) : undefined}
			/>
		{/each}
		<ProviderModelsAvailableControls
			hiddenCount={hiddenAvailableCount}
			visibleCount={visibleAvailable.length}
			availableCount={grouped.available.length}
			showAll={showAllAvailable}
			onShowAll={() => showAllAvailable = true}
			onShowFewer={() => showAllAvailable = false}
		/>
	{/if}
	{#if isCorsiaOmlx}
		<ProviderLocalModelControls onStarted={() => { void loadProviderModelsFor(provider.raw, provider.server); void refreshLocalDownloads(); loadModels(); }} {onAddProvider} corsiaManaged />
	{/if}
{/if}

<style>
	/* The expanded body is a nested list under its provider row: labels, rows and messages start on
	   the provider's name column, and space alone separates them, as between Settings groups.
	   The child components that draw messages and actions share this indent. */
	.section {
		margin-left: calc(var(--space-2) + var(--project-mark-size) + var(--space-3));
		margin-right: var(--space-2);
		padding: var(--space-3) 0 var(--space-1);
		font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary);
	}
	/* LocalSettingsDownloadStatus owns its styles. */
</style>
