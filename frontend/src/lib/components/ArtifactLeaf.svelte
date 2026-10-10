<script lang="ts">
	import { tick } from 'svelte';
	import DocViewer from './DocViewer.svelte';
	import ImageViewer from './ImageViewer.svelte';
	import SubagentConversationLeaf from './SubagentConversationLeaf.svelte';
	import LegacyPlanDiscussionLeaf from './LegacyPlanDiscussionLeaf.svelte';
	import ArtifactTabStrip from './ArtifactTabStrip.svelte';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { TabLoadSignal } from '$lib/artifactTabs';
	import { returnToPlanNode } from '$lib/planLeafReturn';
	import { uiScope } from '$lib/stores/uiScope';
	import { docSaveDrafts, relocateDraftTarget } from '$lib/stores/docSaveQueue';
	import { requestArtifactTabClose } from '$lib/artifactTabClose';
	import {
		threadWorkspaces, emptyWorkspace, getActiveTab, isTabChanged, type ArtifactTab,
		setActiveTab, setActiveSurface, setLeafVisible,
		applyTabLoadSignal, markTabSaved,
	} from '$lib/stores/artifactWorkspace';
	import {
		abandonArtifactOpen, beginArtifactOpen, failArtifactOpen, markArtifactRendered,
	} from '$lib/openTimingRuntime';

	let { threadId, mode, escapeCloses = false, onEscapeClose, onOpenLightbox = () => {} }: {
		threadId: string | null;
		mode: 'rail' | 'expanded' | 'compact';
		/** Compact only: Escape hands the canvas back to chat. */
		escapeCloses?: boolean;
		onEscapeClose?: () => void;
		onOpenLightbox?: (url: string) => void;
	} = $props();

	const workspace = $derived(threadId ? ($threadWorkspaces[threadId] ?? emptyWorkspace()) : emptyWorkspace());
	const activeTab = $derived(getActiveTab(workspace));

	function targetForTab(t: ArtifactTab): DocViewerTarget {
		if (t.kind !== 'document') throw new Error('Only document tabs have DocViewer targets');
		return {
			instanceId: t.instanceId,
			path: t.path,
			name: t.title,
			projectId: $uiScope?.id ?? null,
			threadId: threadId,
			artifactId: t.artifactId,
		};
	}

	// A tab opens as an image when the backend said its file is one (the artifact
	// type, a revision probe, or a text open's typed refusal); everything else
	// opens in the document viewer.
	const showable = $derived<DocViewerTarget | null>(
		activeTab?.kind === 'document' && threadId && activeTab.status !== 'unavailable' ? targetForTab(activeTab) : null
	);
	const isImage = $derived(showable !== null && activeTab?.isImage === true);
	const target = $derived(isImage ? null : showable);
	const imageTarget = $derived(isImage ? showable : null);
	const activeStale = $derived(activeTab ? isTabChanged(activeTab) : false);

	let viewer = $state<DocViewer | undefined>(undefined);
	let imageViewer = $state<ImageViewer | undefined>(undefined);

	// One timing sample per artifact this leaf actually brings on screen (#2280).
	// Keyed on the tab instance, not on re-renders, so a reactive re-run can only
	// ever report the same open once; leaving the leaf abandons what was pending.
	let trackedInstanceId: string | null = null;
	$effect(() => {
		const tab = activeTab;
		const instanceId = tab && tab.kind === 'document' ? tab.instanceId : null;
		if (instanceId === trackedInstanceId) return;
		trackedInstanceId = instanceId;
		if (!tab || instanceId === null) {
			abandonArtifactOpen();
			return;
		}
		beginArtifactOpen(instanceId, tab.kind);
		// An already-loaded tab renders from memory: there is nothing to wait for.
		if (tab.status === 'ready' || tab.status === 'readonly') markArtifactRendered(instanceId);
	});

	// A tab that saved rebaselines on the revision the server returned for its
	// own write, which is what keeps the dot off the saving tab while the same
	// document dots in every other thread that has it open.
	$effect(() => {
		const tid = threadId;
		if (!tid) return;
		const drafts = $docSaveDrafts;
		for (const tab of workspace.tabs) {
			if (tab.kind !== 'document') continue;
			const revision = drafts[tab.instanceId]?.savedRevision;
			if (revision && revision !== tab.loadedRevision) markTabSaved(tid, tab.instanceId, revision);
		}
	});

	// A relocated document keeps editing where it now lives.
	$effect(() => {
		for (const tab of workspace.tabs) {
			if (tab.kind !== 'document') continue;
			const draft = $docSaveDrafts[tab.instanceId];
			if (draft && draft.target.path !== tab.path) relocateDraftTarget(draft.target, tab.path);
		}
	});

	function onLeafClick() {
		if (threadId) setActiveSurface(threadId, 'artifact');
	}
	function onLeafKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter' || event.key === ' ') onLeafClick();
	}
	function onLeafInput() {
		if (threadId) setActiveSurface(threadId, 'artifact');
	}
	function onSignal(signal: TabLoadSignal) {
		if (threadId) applyTabLoadSignal(threadId, signal);
		if (signal.status === 'ready') {
			// The viewer has its content; the DOM update lands on the next tick.
			void tick().then(() => markArtifactRendered(signal.instanceId));
		} else if (signal.status === 'error' || signal.status === 'unavailable') {
			failArtifactOpen(signal.instanceId);
		}
	}
	// Gates the close of ANY tab — active or inactive — on its own pending
	// save: the save queue is keyed by tab instance, not by which tab is
	// currently mounted, so this works identically either way. On failure the
	// failed tab is brought into view (never silently dropped) instead of
	// closed, so the unsaved text and error stay visible and recoverable.
	async function requestCloseTab(instanceId: string) {
		if (!threadId) return;
		const tid = threadId;
		const tab = workspace.tabs.find((t) => t.instanceId === instanceId);
		if (!tab) return;
		await requestArtifactTabClose(tid, tab, () => {
			setActiveTab(tid, instanceId);
			setLeafVisible(tid, true);
			setActiveSurface(tid, 'artifact');
		});
	}
	function selectTab(instanceId: string) {
		if (threadId) setActiveTab(threadId, instanceId);
	}
	function expand() {
		if (!threadId) return;
		setLeafVisible(threadId, true);
		setActiveSurface(threadId, 'artifact');
	}
	function collapse() {
		if (!threadId) return;
		const source = activeTab?.kind === 'subagent' ? activeTab : null;
		setLeafVisible(threadId, false);
		setActiveSurface(threadId, 'chat');
		void returnToPlanNode(source);
	}
</script>

{#if mode === 'rail'}
	<button class="rail" onclick={expand} aria-label="Expand artifact panel ({workspace.tabs.length} open)">
		{#each workspace.tabs as t (t.instanceId)}
			<span class="rail-ico" class:active={t.instanceId === workspace.activeTabId} class:err={t.status === 'error' || t.status === 'unavailable'}>
				<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
				{#if isTabChanged(t)}<span class="rail-dot" aria-hidden="true"></span>{/if}
			</span>
		{/each}
	</button>
{:else}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="leaf" class:compact={mode === 'compact'} onclick={onLeafClick} onkeydown={onLeafKeydown} oninput={onLeafInput}>
		{#if mode === 'expanded'}
			<ArtifactTabStrip
				tabs={workspace.tabs}
				activeKey={workspace.activeTabId}
				onSelect={selectTab}
				onClose={requestCloseTab}
				onCollapse={collapse}
			/>
		{/if}
		<div class="leaf-body" class:document-canvas={!!target} class:image-canvas={!!imageTarget}>
			{#if activeTab?.kind === 'subagent' && activeTab.threadId}
				<SubagentConversationLeaf threadId={activeTab.threadId} {onOpenLightbox} />
				{:else if activeTab?.kind === 'legacy-discussion' && activeTab.artifactId}
					<LegacyPlanDiscussionLeaf artifactId={activeTab.artifactId} title={activeTab.title} />
			{:else if activeTab?.status === 'unavailable'}
				<div class="gone" role="status">
					<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" /></svg>
					<p>{activeTab.title} is no longer available.</p>
					<span>It was deleted, moved out of this scope, or unregistered.</span>
					<button type="button" onclick={() => requestCloseTab(activeTab.instanceId)}>Close tab</button>
				</div>
			{:else if imageTarget}
				<ImageViewer
					bind:this={imageViewer}
					target={imageTarget}
					{onSignal}
					stale={activeStale}
					onReload={() => imageViewer?.reload()}
				/>
			{:else if target}
				<!-- DocViewer owns Escape precedence (comment draft, then selection),
				     so handing it onClose keeps that order intact in compact. -->
				<DocViewer
					bind:this={viewer}
					{target}
					{escapeCloses}
					onClose={() => onEscapeClose?.()}
					{onSignal}
					stale={activeStale}
					onReload={() => viewer?.reload()}
				/>
			{/if}
		</div>
	</div>
{/if}

<style>
	.rail {
		width: 36px; height: 100%; flex-shrink: 0; box-sizing: border-box;
		display: flex; flex-direction: column; align-items: center; gap: 6px;
		padding: calc(var(--header-height, 56px) + 12px) 0 12px;
		border: none; border-left: 1px solid var(--border-subtle);
		background: var(--glass-thin); cursor: pointer;
	}
	.rail:hover { background: var(--bg-hover); }
	.rail-ico {
		position: relative;
		display: flex; align-items: center; justify-content: center;
		width: 24px; height: 24px; border-radius: var(--radius-xs);
		color: var(--text-tertiary);
	}
	.rail-ico.active { color: var(--accent); background: var(--accent-bg); }
	.rail-ico.err { color: var(--red); }
	.rail-dot {
		position: absolute; top: 1px; right: 1px;
		width: 6px; height: 6px; border-radius: var(--radius-circle);
		background: var(--accent);
	}

	.leaf {
		height: 100%; display: flex; flex: 1; flex-direction: column; min-width: 0;
		min-height: 0; overflow: hidden;
		box-sizing: border-box; padding-top: var(--header-height, 56px);
		border-left: 1px solid var(--border-subtle);
		background: var(--bg-primary);
	}
	/* Compact fills the canvas, so it owns no divider edge and the tab strip's
	   role is taken over by the app header. */
	.leaf.compact { flex: 1; border-left: none; }
	.leaf-body { flex: 1; min-height: 0; overflow-y: auto; padding: 8px 20px 20px; }
	.leaf-body.document-canvas { overflow: hidden; container: doc-canvas / inline-size; }
	.leaf-body.image-canvas { overflow: hidden; display: flex; flex-direction: column; }

	.gone {
		display: flex; flex-direction: column; align-items: flex-start; gap: 6px;
		padding: 28px 4px; color: var(--text-tertiary);
	}
	.gone p { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-medium); color: var(--text-secondary); }
	.gone span { font-size: var(--text-footnote); line-height: var(--lh-snug); }
	.gone button {
		margin-top: 8px; font: inherit; font-size: var(--text-caption); font-weight: var(--fw-semibold);
		color: var(--text-primary); background: var(--bg-hover);
		border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
		padding: 6px 12px; cursor: pointer;
	}
	.gone button:hover { border-color: var(--accent); color: var(--accent); }
</style>
