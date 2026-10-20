<script lang="ts">
	import ContextSheet from './ContextSheet.svelte';
	import ContextSourceDetail from './ContextSourceDetail.svelte';
	import InputBarActionRow from './InputBarActionRow.svelte';
	import InputBarModelSheet from './InputBarModelSheet.svelte';
	import InputBarThinkingSheet from './InputBarThinkingSheet.svelte';
	import LimitHeldLine from './LimitHeldLine.svelte';
	import { checkpointsByThread, latestCheckpoint, requestManualCheckpoint } from '$lib/stores/checkpoints';
	import { activeThreadId, reasoningEffort, threads, type ContextUsage } from '$lib/stores/threads';
	import {
		contextPreviewByThread,
		contextReading,
		contextUsageByThread,
		ensureContextPreview,
		hasMeasuredNumbers,
		measuredFor,
		measuredHydratedThreads,
		previewIdentity,
	} from '$lib/stores/contextUsageByThread';
	import { acceptsSend, activeModelId, findModelById, loadModels, models } from '$lib/stores/models';
	import { canonicalName } from '$lib/stores/modelGroups';
	import { subagentMeta } from '$lib/stores/subagents';
	import { reasoningEffortLabel, resolveReasoningEffort } from './inputBarReasoning';
	import { token } from '$lib/stores/auth';
	import { contextSourceOwnerTarget } from '$lib/contextSourceNavigation';
	import { openArtifactAndShowThread } from '$lib/openArtifactInThread';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import { uiScope } from '$lib/stores/uiScope';
	import { openProjectSettings, requestSettingsSection } from '$lib/stores/settingsNavigation';
	import { toasts } from '$lib/stores/toast';
	import {
		contextSheetUsageSummary,
		type ContextSheetSourceRow,
		type ContextSheetUsageSummary,
	} from './contextSheetModel';
	import type { SheetView } from './inputBarHelpers';
	let {
		threadId = undefined,
		surface,
		sheetView,
		toggleSheet,
		isMobile,
		hasImageAttachments,
		inputText,
		pendingAttachmentCount,
		hasMessageToSend,
		canSendToModel,
		queueUntilSwitch,
		focusLocalOnly = false,
		onfocusconsumed = () => {},
		onAttachFile,
		onAttachCamera,
		onAttachPhotos,
		onSendClick,
		onQueueAfterTurn,
	}: {
		threadId?: string | null;
		surface: 'sheets' | 'row';
		sheetView: SheetView;
		toggleSheet: (view: Exclude<SheetView, null>) => void;
		isMobile: boolean;
		hasImageAttachments: boolean;
		inputText: string;
		pendingAttachmentCount: number;
		hasMessageToSend: boolean;
		canSendToModel: boolean;
		queueUntilSwitch: boolean;
		focusLocalOnly?: boolean;
		onfocusconsumed?: () => void;
		onAttachFile: () => void;
		onAttachCamera: () => void;
		onAttachPhotos: () => void;
		onSendClick: () => void;
		onQueueAfterTurn: () => void;
	} = $props();

	const activeModel = $derived(findModelById($models, $activeModelId));
	// The chip reads as chosen under the composer's one send rule (`acceptsSend`): a usage-limit block
	// is a normal chip, since the held line says why. Any other block (not offered, signed out,
	// model gone) is still 'Unavailable'.
	const chipReady = $derived(activeModel !== undefined && acceptsSend(activeModel));
	const modelName = $derived(
		chipReady
			? canonicalName(activeModel!)
			: $activeModelId
				? `Unavailable · ${activeModel ? canonicalName(activeModel) : $activeModelId}`
				: 'Select Model'
	);
	// Defaults to the active thread so a caller that predates the subagent
	// leaf (#1949) keeps its existing behavior unchanged.
	const boundThreadId = $derived(threadId ?? $activeThreadId);
	// Context usage and the next-turn preview are keyed by the bound thread
	// (#2269), so every composer inspects its own thread: the active thread with
	// the selected model, a subagent leaf (#1949) with the worker's own model.
	const isActiveThread = $derived(boundThreadId !== null && boundThreadId === $activeThreadId);
	const previewModelId = $derived(isActiveThread ? $activeModelId : null);
	const canInspectContext = $derived(
		boundThreadId !== null && (!isActiveThread || $activeModelId !== null),
	);
	const boundUsage = $derived(boundThreadId ? $contextUsageByThread.get(boundThreadId) ?? null : null);
	const boundPreview = $derived(boundThreadId ? $contextPreviewByThread.get(boundThreadId) : undefined);
	const subagentRef = $derived(boundThreadId ? $subagentMeta.get(boundThreadId) : undefined);
	// Two rings can be on screen, so each names its thread.
	const contextThreadName = $derived(
		subagentRef?.name
			?? $threads.find(thread => thread.id === boundThreadId)?.title
			?? 'this thread',
	);
	const isSubagentThread = $derived(subagentRef !== undefined);
	const isCCBacked = $derived(isSubagentThread || activeModel?.backend === 'cc');
	const showVisionWarning = $derived(
		hasImageAttachments &&
		!isCCBacked &&
		activeModel !== undefined &&
		activeModel.capabilities !== undefined &&
		!activeModel.capabilities.supports_vision
	);

	const reasoningConfig = $derived(activeModel?.capabilities?.reasoning_config ?? null);
	const supportsReasoning = $derived(reasoningConfig !== null);
	const resolvedEffort = $derived(resolveReasoningEffort($reasoningEffort, reasoningConfig));
	const effortLabel = $derived.by(() => {
		if (!supportsReasoning) return '';
		return reasoningEffortLabel(reasoningConfig, resolvedEffort);
	});
	let contextSourceDetail = $state<ContextSheetSourceRow | null>(null);
	// The opened source groups of the context view now on screen. This component
	// stays mounted across preview refreshes, which can leave the sheet with no
	// snapshot for a moment, so the record lives here, not in the list. It starts
	// empty for every view: closing the view or changing the bound thread drops it.
	let expandedGroups = $state<Record<string, boolean>>({});
	const contextViewKey = $derived(sheetView === 'context' ? boundThreadId : null);
	$effect.pre(() => {
		void contextViewKey;
		expandedGroups = {};
	});
	function toggleGroup(groupId: string, open: boolean) {
		expandedGroups[groupId] = open;
	}
	function chipSummaryFor(
		usage: ContextUsage | null,
	): Pick<
		ContextSheetUsageSummary,
		'pct' | 'utilization' | 'tokenSource' | 'countsAgainstBudget'
	> | null {
		if (!usage) return null;
		// One computation for both surfaces. The chip previously rounded the
		// backend's stored utilization, which is itself already rounded to three
		// decimals, while the sheet recomputed from raw tokens — so the two could
		// differ by a point for the same usage (#1413). Deriving both from
		// contextSheetUsageSummary also keeps the chip inside its measured-only
		// gate, so an estimate cannot render through the measured numeric path.
		const summary = contextSheetUsageSummary(usage);
		return {
			pct: summary.pct,
			utilization: summary.utilization,
			tokenSource: summary.tokenSource,
			countsAgainstBudget: summary.countsAgainstBudget,
		};
	}

	const checkpointState = $derived(
		boundThreadId ? $checkpointsByThread.get(boundThreadId) : undefined,
	);
	const previewIds = $derived(
		boundThreadId
			? previewIdentity(boundThreadId, previewModelId, boundUsage, latestCheckpoint(checkpointState))
			: null,
	);
	const measuredReading = $derived(measuredFor(boundUsage, boundThreadId, previewModelId, $models));
	// The preview read for this thread, model and measured turn, if any; a
	// newer measured turn or another model leaves it unowned and unshown.
	const ownedPreview = $derived(
		previewIds && boundPreview?.owner === previewIds.owner ? boundPreview : undefined,
	);
	// One reading for the donut and the sheet headline (#1952): the owned
	// preview, else the last measured usage, so opening never waits on the
	// fetch (#1953) and closing the sheet changes nothing.
	const sheetUsage = $derived(
		previewIds ? contextReading(ownedPreview, previewIds.owner, measuredReading) : null,
	);
	const contextChipSummary = $derived(chipSummaryFor(sheetUsage));
	const contextPreviewLoading = $derived(ownedPreview?.loading ?? false);
	const contextPreviewError = $derived(ownedPreview?.error ?? '');
	let sheetWasOpen = false;
	$effect(() => {
		const t = $token;
		const ids = previewIds;
		const sheetOpen = surface === 'sheets' && sheetView === 'context';
		const opening = sheetOpen && !sheetWasOpen;
		sheetWasOpen = sheetOpen;
		// The always-mounted sheets instance is the one reader per composer.
		if (surface !== 'sheets' || !boundThreadId || !canInspectContext || !t || !ids) return;
		if (opening) contextSourceDetail = null;
		if (!sheetOpen) {
			// Closed, the preview is read only so the donut is not empty: once the
			// thread's first page is read and holds no usable receipt (a reading
			// kept past a turn that could not measure is not one), and once
			// per owner, so a failed read is not repeated.
			const hydrated = $measuredHydratedThreads.has(boundThreadId);
			const usable = hasMeasuredNumbers(measuredReading) && !measuredReading?.stale;
			if (!hydrated || usable || ownedPreview) return;
		}
		void ensureContextPreview({
			threadId: boundThreadId,
			authToken: t,
			modelId: previewModelId,
			...ids,
			retryError: opening,
		});
	});
	function openSheet(view: Exclude<SheetView, null>) {
		if (sheetView !== view && view === 'model') loadModels({ refreshUsage: true });
		toggleSheet(view);
	}
	function documentName(source: ContextSheetSourceRow): string {
		const parts = source.stableReference.split(/[\\/]/).filter(Boolean);
		return parts.at(-1) ?? source.label ?? 'Document';
	}

	function openContextSource(source: ContextSheetSourceRow) {
		const target = contextSourceOwnerTarget(source);
		if (target === 'project-settings') {
			if ($uiScope) openProjectSettings($uiScope.id);
			else requestSettingsSection('projects');
		} else if (target === 'thread-settings') {
			threadSettingsOpen.set(true);
		} else if (target === 'workspace-settings') {
			requestSettingsSection('context');
		} else if (target === 'providers') {
			requestSettingsSection('providers');
		} else if (target === 'system') {
			requestSettingsSection('system');
		} else if (target === 'runtime-details' || target === null) {
			// A row with no owner surface to open still explains itself.
			// A detail visit is not part of the refresh the record survives.
			expandedGroups = {};
			contextSourceDetail = source;
			return;
		} else if (target === 'document') {
			openArtifactAndShowThread(
				{ path: source.stableReference, title: documentName(source) },
				boundThreadId,
			);
		}
		toggleSheet('context');
	}

	async function checkpointNow() {
		const authToken = $token;
		if (!boundThreadId || !authToken) return;
		try {
			await requestManualCheckpoint(boundThreadId, authToken);
		} catch (error) {
			// The sheet only offers this button when the backend already says a
			// checkpoint is possible, so a refusal here means the preview went
			// stale between fetch and click — worth a toast either way (#1952).
			toasts.show(
				error instanceof Error
					? error.message
					: 'Could not start checkpoint',
				'error',
			);
		}
	}
</script>

{#if surface === 'sheets'}
<div>
{#if showVisionWarning}
	<div class="vision-warning">
		<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
		<span>{activeModel?.name ?? 'Current model'} doesn't support images — switch to a vision-capable model to use image attachments.</span>
	</div>
{/if}
<LimitHeldLine model={activeModel} onSwitchModel={() => openSheet('model')} />

{#if sheetView === 'attachments'}
	<div class="action-sheet">
		{#if isMobile}
			<div class="mobile-attach-row">
				<button class="mobile-attach-btn" onclick={onAttachCamera}>
					<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg>
					Camera
				</button>
				<button class="mobile-attach-btn" onclick={onAttachPhotos}>
					<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
					Photos
				</button>
				<button class="mobile-attach-btn" onclick={onAttachFile}>
					<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
					Files
				</button>
			</div>
		{:else}
			<button class="sheet-item press-tint" onclick={onAttachFile}>
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
				<span class="sheet-label">Attach file</span>
			</button>
		{/if}
	</div>
{/if}

{#if sheetView === 'model'}
	<InputBarModelSheet onclose={() => toggleSheet('model')} {focusLocalOnly} onfocusconsumed={onfocusconsumed} />
{/if}

{#if sheetView === 'context'}
	<div class="action-sheet context-sheet" role="group" aria-label={`Context for ${contextThreadName}`}>
		{#if contextSourceDetail}
			<ContextSourceDetail source={contextSourceDetail} onBack={() => { contextSourceDetail = null; }} />
		{/if}
			{#if !contextSourceDetail && (sheetUsage || checkpointState)}
				<ContextSheet
					usage={sheetUsage} loading={contextPreviewLoading}
					refreshFailed={contextPreviewError}
					onOpenSource={openContextSource}
					{checkpointState}
					threadId={boundThreadId}
					threadLabel={contextThreadName}
					onCheckpointNow={checkpointNow}
					{expandedGroups}
					onToggleGroup={toggleGroup}
				/>
			{:else if !contextSourceDetail && contextPreviewLoading}
				<div class="sheet-empty">Loading context...</div>
			{:else if !contextSourceDetail}
				<div class="sheet-empty">{contextPreviewError || 'No context available'}</div>
			{/if}
	</div>
{/if}

{#if sheetView === 'thinking' && supportsReasoning && reasoningConfig}
	<InputBarThinkingSheet {reasoningConfig} />
{/if}
</div>
{/if}

{#if surface === 'row'}
	<InputBarActionRow
		threadId={boundThreadId}
		{sheetView}
		{openSheet}
		{isSubagentThread}
		{subagentRef}
		isActiveModelReady={chipReady}
		{canSendToModel}
		{queueUntilSwitch}
		modelLabel={modelName}
		activeModelName={activeModel?.name}
		{supportsReasoning}
		{effortLabel}
		{canInspectContext}
		contextThreadName={contextThreadName}
		contextUtilization={contextChipSummary?.utilization ?? null}
		contextPct={contextChipSummary?.pct ?? null}
		contextTokenSource={contextChipSummary?.tokenSource ?? null}
		contextCountsAgainstBudget={contextChipSummary?.countsAgainstBudget ?? false}
		{inputText}
		{pendingAttachmentCount}
		{hasMessageToSend}
		{onSendClick}
			{onQueueAfterTurn}
	/>
{/if}
