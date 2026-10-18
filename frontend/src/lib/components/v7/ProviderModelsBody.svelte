<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { get } from 'svelte/store';
	import { fetchLocalModelRecommendations, fetchSources } from '$lib/conductorSources';
	import {
		providerModels,
		loadProviderModelsFor,
		pollJobsFor,
		loadModel,
		unloadModel,
		modelsForProvider,
		markModelOptimistic,
		cancelJob,
		type ProviderModel,
	} from '$lib/stores/providerModels';
	import { loadModels, workspaceModels as catalogModels } from '$lib/stores/models';
	import { chatPickerWarning } from './modelWarning';
	import { omlxAdminUrl } from '$lib/stores/providerModelUtils';
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

	type PreloadInfo = {
		modelName: string;
		estimatedGb: number;
		freeGb: number;
		afterLoadGb: number;
		safeReserveGb: number;
		warning: string;
		onConfirm: () => Promise<void>;
	};
	let preloadInfo = $state<PreloadInfo | null>(null);
	let detailsFor = $state<ProviderModel | null>(null);
	let deleteCandidate = $state<ProviderModel | null>(null);

	const POLL_ACTIVE_MS = 250;
	const POLL_IDLE_MS = 5000;
	const POLL_QUIET_TIMEOUT_MS = 30_000;
	let pollHandle: ReturnType<typeof setTimeout> | null = null;
	let stopLocalUpdates: (() => void) | null = null;
	let lastActiveAt = Date.now();
	let polling = false;

	let showAllAvailable = $state(false);
	let actionError = $state('');
	let hasLocalDownload = $state(false);
	let downloadSpeeds = $state(new Map<string, number>());

	const pmState = $derived(modelsForProvider($providerModels, provider.raw, provider.server));
	const models = $derived(pmState.models);

	const grouped = $derived.by(() => {
		const loaded: ProviderModel[] = [];
		const loading: ProviderModel[] = [];
		const available: ProviderModel[] = [];
		for (const m of models) {
			if (m.state === 'loaded') loaded.push(m);
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
	const adminUrl = $derived(provider.name === 'omlx' ? omlxAdminUrl(provider.server?.mgmt_base ?? undefined) : undefined);
	const catalogOnly = $derived(provider.type === 'cloud' || isExternal || isHarness);
	// A harness section names the switch, not picker delivery; the row warning says when the picker can't offer it.
	const loadedLabel = $derived(isHarness ? 'Shown' : catalogOnly ? 'Active' : 'Loaded');
	const availableLabel = $derived(isHarness ? 'Hidden' : 'Available');
	// The switch's name says what it does here: nothing loads unless a local engine runs the model.
	const toggleLabels = $derived<[string, string]>(
		isHarness ? ['Hide from picker', 'Show in picker'] : catalogOnly ? ['Turn off', 'Turn on'] : ['Unload', 'Load'],
	);

	function _hasTransitionalModel(): boolean {
		for (const m of models) {
			if (m.state === 'loading' || m.state === 'cache_warming' || m.state === 'queued') {
				return true;
			}
		}
		return false;
	}

	async function _pollTick() {
		if (!polling) return;
		await pollJobsFor(provider.raw, provider.server);
		const active = _hasTransitionalModel();
		if (active) lastActiveAt = Date.now();
		const quietFor = Date.now() - lastActiveAt;
		if (quietFor > POLL_QUIET_TIMEOUT_MS) {
			polling = false;
			pollHandle = null;
			return;
		}
		const next = active ? POLL_ACTIVE_MS : POLL_IDLE_MS;
		pollHandle = setTimeout(_pollTick, next);
	}

	function _ensurePolling() {
		if (polling) return;
		polling = true;
		lastActiveAt = Date.now();
		pollHandle = setTimeout(_pollTick, POLL_ACTIVE_MS);
	}
	async function refreshLocalDownload() {
		if (provider.name !== 'omlx') return;
		const t = get(token);
		if (!t) return;
		try {
			hasLocalDownload = (await fetchSources(t)).some((source) => source.source === 'omlx' && source.settings_download);
		} catch { /* Keep the last known download state until the next source update. */ }
	}

	async function refreshLocalDownloadSpeeds() {
		if (provider.name !== 'omlx' || !provider.server?.id) return;
		const t = get(token);
		if (!t) return;
		try {
			const body = await fetchLocalModelRecommendations(t);
			const entries = (body.downloads ?? []).filter((entry) =>
				entry.status === 'available' && entry.server_id === provider.server?.id &&
				typeof entry.speed === 'number' && Number.isFinite(entry.speed),
			);
			downloadSpeeds = new Map(entries.map((entry) => [entry.repository.split('/').at(-1)!, entry.speed!]));
		} catch { /* The canonical server rows remain useful without optional measured speed. */ }
	}

	function downloadSpeed(model: ProviderModel): number | null {
		return downloadSpeeds.get(model.id) ?? null;
	}

	function displayMeta(model: ProviderModel): string {
		const speed = downloadSpeed(model);
		if (speed === null) return model.meta;
		const shown = Number.isInteger(speed) ? String(speed) : String(Number(speed.toFixed(1)));
		return [model.meta, `${shown} tokens/s`].filter(Boolean).join(' · ');
	}

	onMount(() => {
		void Promise.all([loadProviderModelsFor(provider.raw, provider.server), refreshLocalDownload(), refreshLocalDownloadSpeeds()]).then(() => {
			if (!catalogOnly) {
				void pollJobsFor(provider.raw, provider.server);
				_ensurePolling();
			}
		});
		if (provider.name === 'omlx') {
			stopLocalUpdates = on('models_updated', () => {
				void Promise.all([loadProviderModelsFor(provider.raw, provider.server), refreshLocalDownload(), refreshLocalDownloadSpeeds()]);
			});
		}
	});

	onDestroy(() => {
		polling = false;
		if (pollHandle) clearTimeout(pollHandle);
		stopLocalUpdates?.();
		// The extracted Settings download status cleans up its own subscription.
	});

	// A saved checkpoint window can make a row usable; show the new window at once.
	async function _windowChanged() {
		await loadProviderModelsFor(provider.raw, provider.server);
		loadModels();
		if (detailsFor) detailsFor = models.find((m) => m.id === detailsFor?.id) ?? detailsFor;
	}

	async function _doLoad(m: ProviderModel) {
		const r = await loadModel(provider.raw, provider.server, m.id, m.dbId);
		if (!r.ok) {
			actionError = r.message ?? 'Failed';
			return;
		}
		await loadProviderModelsFor(provider.raw, provider.server);
		loadModels();
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

	async function _checkAndLoad(m: ProviderModel) {
		const estimatedGb = m.sizeBytes ? m.sizeBytes / (1024 ** 3) : 0;
		if (!isLocal || estimatedGb <= 0) {
			await _doLoad(m);
			return;
		}
		try {
			const t = get(token) || '';
			const res = await fetch(`/api/system/host-resources/check-load?token=${t}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ estimated_gb: estimatedGb, provider: provider.name }),
			});
			const data = await res.json();
			if (data.ok || data.remote) {
				await _doLoad(m);
				return;
			}
			preloadInfo = {
				modelName: m.name,
				estimatedGb,
				freeGb: data.free_gb ?? 0,
				afterLoadGb: data.after_load_gb ?? 0,
				safeReserveGb: data.safe_reserve_gb ?? 0,
				warning: data.warning ?? 'Loading may exceed safe memory.',
				onConfirm: async () => {
					preloadInfo = null;
					await _doLoad(m);
				},
			};
		} catch {
			await _doLoad(m);
		}
	}

	async function handleToggleModel(m: ProviderModel) {
		actionError = '';
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
		onCancel={() => preloadInfo = null}
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
	{#if provider.name === 'omlx'}
		<ProviderLocalModelControls onStarted={() => { void loadProviderModelsFor(provider.raw, provider.server); void refreshLocalDownload(); loadModels(); }} {onAddProvider} empty />
	{/if}
{:else}
	{#if actionError}
		<ProviderModelsActionError message={actionError} />
	{/if}

	{#if grouped.loaded.length > 0}
		<div class="section loaded">
			<span class="section-label">{loadedLabel}</span>
			<span class="section-rule"></span>
		</div>
		{#each grouped.loaded as m (m.id)}
			<ModelRow
				name={m.name}
				state={m.state}
				meta={displayMeta(m)}
				caps={m.caps}
				warning={chatPickerWarning($catalogModels, m.dbId, isExternal)}
				toggleOn={true}
				{toggleLabels}
				onToggle={() => handleToggleModel(m)}
				onInfo={() => detailsFor = m}
				onDeleteDownload={provider.name === 'omlx' ? () => requestDownloadDeletion(m) : undefined}
			/>
		{/each}
	{/if}

	{#if grouped.loading.length > 0}
		<div class="section loading">
			<span class="section-label">Loading</span>
			<span class="section-rule"></span>
		</div>
		{#each grouped.loading as m (m.id)}
			<ModelRow
				name={m.name}
				state={m.state}
				meta={displayMeta(m)}
				caps={m.caps}
				progress={m.progress}
				onCancel={() => handleCancel(m)}
			/>
		{/each}
	{/if}

	{#if grouped.available.length > 0 || (provider.name === 'omlx' && hasLocalDownload)}
		<div class="section">
			<span class="section-label">{availableLabel}</span>
			<span class="section-rule"></span>
		</div>
		{#if provider.name === 'omlx'}
			<LocalSettingsDownloadStatus onStatusChange={(hasDownload) => hasLocalDownload = hasDownload} />
		{/if}
		{#each visibleAvailable as m (m.id)}
			<ModelRow
				name={m.name}
				state={m.state}
				meta={displayMeta(m)}
				caps={m.caps}
				note={downloadSpeed(m) !== null ? 'Settings measured on Macs like this one · omlx.ai' : null}
				toggleOn={false}
				{toggleLabels}
				onToggle={() => handleToggleModel(m)}
				onInfo={() => detailsFor = m}
				onRetry={m.state === 'error' ? () => handleRetry(m) : undefined}
				onDeleteDownload={provider.name === 'omlx' ? () => requestDownloadDeletion(m) : undefined}
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
	{#if provider.name === 'omlx'}
		<ProviderLocalModelControls onStarted={() => { void loadProviderModelsFor(provider.raw, provider.server); void refreshLocalDownload(); loadModels(); }} {onAddProvider} />
	{/if}
{/if}


<style>
	.section {
		display: flex; align-items: center; gap: 8px;
		padding: 14px 18px 8px;
	}
	.section-label {
		font-size: var(--text-caption); font-weight: 700; letter-spacing: 0.12em;
		color: var(--text-tertiary); text-transform: uppercase;
	}
	.section-rule {
		flex: 1; height: 1px;
		background: linear-gradient(90deg, var(--border-subtle) 30%, transparent);
	}
	.section.loaded .section-label  { color: var(--accent-text); }
	.section.loading .section-label { color: var(--amber); }


	@media (max-width: 480px) {
		.section { padding: 10px 12px 6px; }
	}
	/* LocalSettingsDownloadStatus owns its styles. */

</style>
