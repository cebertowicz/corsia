<script lang="ts">
	import { tick, untrack } from 'svelte';
	import DocViewer from './DocViewer.svelte';
	import ImageViewer from './ImageViewer.svelte';
	import ArtifactDocumentImageOverlay from './ArtifactDocumentImageOverlay.svelte';
	import SubagentConversationLeaf from './SubagentConversationLeaf.svelte';
	import LegacyPlanDiscussionLeaf from './LegacyPlanDiscussionLeaf.svelte';
	import BrowserLeaf from './BrowserLeaf.svelte';
	import ArtifactTabStrip from './ArtifactTabStrip.svelte';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { ImageViewportPlace, TabLoadSignal } from '$lib/artifactTabs';
	import { returnToPlanNode } from '$lib/planLeafReturn';
	import { pushBackLayer } from '$lib/historyBackLayer';
	import type { DocumentImageRequest } from '$lib/docViewerImages';
	import { widenForImageViewer } from '$lib/imageViewerPane';
	import { restoreDocumentPlace, type RestoreDocumentPlaceOptions, type TextDocumentPlace } from '$lib/documentImagePlace';
	import { uiScope } from '$lib/stores/uiScope';
	import { docSaveDrafts, relocateDraftTarget } from '$lib/stores/docSaveQueue';
	import { requestArtifactTabClose } from '$lib/artifactTabClose';
	import {
		threadWorkspaces, emptyWorkspace, getActiveTab, isTabChanged, type ArtifactTab,
		setActiveTab, setActiveSurface, setLeafVisible, applyTabLoadSignal, markTabSaved,
	} from '$lib/stores/artifactWorkspace';
	import {
		tabViewState, tabDocumentImage, recordTabDocumentPlace, recordTabImagePlace, openTabDocumentImage,
		recordTabDocumentImageCamera, clearTabDocumentImage,
	} from '$lib/stores/tabViewState';
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

	// Every tab opens in the document viewer. A file the backend says is an image
	// is switched to the image viewer for as long as this leaf is mounted; nothing
	// about it is stored on the tab, so a file replaced by one of the other kind
	// is shown by whichever viewer the next load finds it needs.
	let imageTabs = $state<string[]>([]);
	const imageKey = (instanceId: string) => `${threadId}\u0000${instanceId}`;
	const imageTabIds = $derived(new Set(workspace.tabs.filter((t) => imageTabs.includes(imageKey(t.instanceId))).map((t) => t.instanceId)));
	const showable = $derived<DocViewerTarget | null>(
		activeTab?.kind === 'document' && threadId && activeTab.status !== 'unavailable' ? targetForTab(activeTab) : null
	);
	const showsImage = $derived(showable !== null && !!activeTab && imageTabs.includes(imageKey(activeTab.instanceId)));
	const target = $derived(showsImage ? null : showable);
	const imageTarget = $derived(showsImage ? showable : null);
	const activeStale = $derived(activeTab ? isTabChanged(activeTab) : false);
	const activeViewState = $derived(threadId && activeTab ? tabViewState(threadId, activeTab.instanceId) : null);

	let viewer = $state<DocViewer | undefined>(undefined);
	let imageViewer = $state<ImageViewer | undefined>(undefined);
	let leafEl = $state<HTMLElement | undefined>(undefined);
	let leafBodyEl = $state<HTMLElement | undefined>(undefined);
	/** The tab whose place is being put back; a document not on screen yet stays covered until it is in place. */
	let restoringDocument = $state<{ instanceId: string; covered: boolean } | null>(null);
	let documentRestore: AbortController | null = null;
	function restoreDocument(place: Parameters<typeof restoreDocumentPlace>[1], options: RestoreDocumentPlaceOptions = {}) {
		documentRestore?.abort();
		const controller = new AbortController();
		documentRestore = controller;
		void restoreDocumentPlace(leafBodyEl, place, { ...options, signal: controller.signal }).finally(() => {
			if (documentRestore === controller) documentRestore = null;
		});
	}
	// An image of the open document shown larger. Its tab's view state is the one record of it and its camera, so a
	// tab switch keeps it with its tab. The document stays mounted beneath it, so closing it returns to the same live
	// document; its comments are taken from that document each time it shows, never kept with the image.
	const shownImage = $derived(threadId && target ? tabDocumentImage(threadId, target.instanceId) : null);
	const imageOpen = $derived(shownImage !== null && mode !== 'rail');
	const documentCovered = $derived(imageOpen || (restoringDocument?.covered === true && restoringDocument.instanceId === target?.instanceId));
	const imageComments = $derived(imageOpen && viewer ? viewer.imageComments() : undefined);
	// Opening an image from its document eases the canvas wider and Back eases it back. Any other change (a tab switch,
	// or a return to a tab whose image is open) puts the canvas at its width at once, in the frame the tab turns active.
	let easeCanvas = false;
	function openImage(request: DocumentImageRequest) {
		easeCanvas = true;
		if (threadId && target) openTabDocumentImage(threadId, target.instanceId, request);
	}
	function closeImage() {
		const image = shownImage;
		if (!threadId || !target || !image) return;
		easeCanvas = true;
		restoringDocument = null;
		clearTabDocumentImage(threadId, target.instanceId);
		restoreDocument(image);
	}
	// Collapsing the pane to its rail closes the image; a tab switch, or leaving for chat, keeps it with its tab.
	$effect(() => {
		const tid = threadId, instanceId = activeTab?.instanceId;
		if (mode === 'rail' && tid && instanceId) untrack(() => clearTabDocumentImage(tid, instanceId));
	});
	$effect(() => () => documentRestore?.abort());
	$effect(() => {
		if (restoringDocument === null || restoringDocument.instanceId === activeTab?.instanceId) return;
		documentRestore?.abort();
		restoringDocument = null;
	});
	$effect(() => {
		if (!imageOpen || mode !== 'expanded') return;
		const release = widenForImageViewer(untrack(() => leafEl?.getBoundingClientRect().width ?? 0), { instant: !easeCanvas });
		easeCanvas = false;
		return () => { release({ instant: !easeCanvas }); easeCanvas = false; };
	});
	// Back (a phone's or the browser's) closes the viewer before it leaves the document.
	$effect(() => {
		if (!imageOpen) return;
		return pushBackLayer(() => { closeImage(); return true; });
	});

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
	// Forget this thread's image tabs that were closed.
	$effect(() => {
		const open = new Set(workspace.tabs.map((t) => imageKey(t.instanceId)));
		const prefix = `${threadId}\u0000`;
		const kept = imageTabs.filter((k) => !k.startsWith(prefix) || open.has(k));
		if (kept.length !== imageTabs.length) imageTabs = kept;
	});
	function recordDocumentPlace(instanceId: string, place: TextDocumentPlace) {
		if (threadId) recordTabDocumentPlace(threadId, instanceId, place);
	}
	function recordImagePlace(instanceId: string, place: ImageViewportPlace) {
		if (threadId) recordTabImagePlace(threadId, instanceId, place);
	}
	function recordDocumentImageCamera(instanceId: string, camera: ImageViewportPlace) {
		if (threadId) recordTabDocumentImageCamera(threadId, instanceId, camera);
	}
	function onSignal(signal: TabLoadSignal) {
		if (signal.renderer) {
			const key = imageKey(signal.instanceId);
			const others = imageTabs.filter((k) => k !== key);
			imageTabs = signal.renderer === 'image' ? [...others, key] : others;
		}
		if (threadId) applyTabLoadSignal(threadId, signal);
		if (signal.status === 'ready') {
			// The viewer has its content; the DOM update lands on the next tick.
			void tick().then(() => markArtifactRendered(signal.instanceId));
			const place = threadId && activeTab?.instanceId === signal.instanceId
				? tabViewState(threadId, signal.instanceId)?.documentPlace
				: null;
			if (place) {
				restoringDocument = { instanceId: signal.instanceId, covered: !signal.shown };
				restoreDocument(place, {
					mode: 'tab-return',
					onPlaced: () => {
						if (restoringDocument?.instanceId === signal.instanceId && activeTab?.instanceId === signal.instanceId) restoringDocument = null;
					},
				});
			}
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
				{#if imageTabIds.has(t.instanceId)}
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m21 16-5-5-8 9" /></svg>
				{:else}
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
				{/if}
				{#if isTabChanged(t)}<span class="rail-dot" aria-hidden="true"></span>{/if}
			</span>
		{/each}
	</button>
{:else}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="leaf" class:compact={mode === 'compact'} bind:this={leafEl} onclick={onLeafClick} onkeydown={onLeafKeydown} oninput={onLeafInput}>
		{#if mode === 'expanded'}
			<div class="canvas-header-strip">
				<ArtifactTabStrip
					tabs={workspace.tabs}
					activeKey={workspace.activeTabId}
					{imageTabIds}
					onSelect={selectTab}
					onClose={requestCloseTab}
					onCollapse={collapse}
					showCollapse={false}
				/>
			</div>
		{/if}
		<div class="leaf-body" class:document-canvas={!!target} class:image-canvas={!!imageTarget} class:browser-canvas={activeTab?.kind === 'browser'} bind:this={leafBodyEl}>
			{#if activeTab?.kind === 'browser' && activeTab.threadId}
				<BrowserLeaf threadId={activeTab.threadId} />
				{:else if activeTab?.kind === 'subagent' && activeTab.threadId}
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
					place={activeViewState?.imagePlace ?? null}
					onPlace={(place) => recordImagePlace(imageTarget.instanceId, place)}
					{onSignal}
					stale={activeStale}
					onReload={() => imageViewer?.reload()}
				/>
			{:else if target}
				<div class="doc-host" class:covered={documentCovered} inert={documentCovered}>
					<DocViewer
						bind:this={viewer}
						{target}
						{escapeCloses}
						onClose={() => onEscapeClose?.()}
						{onSignal}
						stale={activeStale}
						onReload={() => viewer?.reload()}
						onOpenImage={openImage}
						onDocumentPlace={recordDocumentPlace}
						covered={imageOpen}
					/>
				</div>
				{#if imageOpen && shownImage && viewer}
					<ArtifactDocumentImageOverlay
						image={shownImage}
						comments={imageComments}
						{target}
						place={shownImage.camera ?? null}
						onBack={closeImage}
						onPlace={(camera) => recordDocumentImageCamera(target.instanceId, camera)}
					/>
				{/if}
			{/if}
		</div>
	</div>
{/if}

<style>
	.rail {
		width: 36px; height: 100%; flex-shrink: 0; box-sizing: border-box;
		display: flex; flex-direction: column; align-items: center; gap: 6px;
		padding: calc(var(--header-bottom) + 12px) 0 12px;
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
	.rail-ico.active { color: var(--violet-text); background: var(--accent-bg); }
	.rail-ico.err { color: var(--red); }
	.rail-dot {
		position: absolute; top: 1px; right: 1px;
		width: 6px; height: 6px; border-radius: var(--radius-circle);
		background: var(--accent);
	}
	.leaf {
		height: 100%; display: flex; flex: 1; flex-direction: column; min-width: 0;
		min-height: 0; overflow: hidden; position: relative;
		box-sizing: border-box; padding-top: var(--header-bottom);
		border-left: none;
		background: var(--bg-primary);
	}
	.leaf.compact { flex: 1; border-left: none; }
	.canvas-header-strip {
		position: absolute; inset: 0 88px auto 0; z-index: 11;
		height: var(--header-bottom);
	}
	.canvas-header-strip :global(.strip) {
		height: 100%; padding-left: 10px; padding-right: 6px;
		border-bottom: 0; background: transparent;
	}
	.leaf-body { flex: 1; min-height: 0; overflow-y: auto; padding: 8px 20px 20px; }
	.leaf-body.document-canvas, .leaf-body.image-canvas { overflow: hidden; container: doc-canvas / inline-size; padding-top: 0; }
	.leaf-body.document-canvas { position: relative; }
	.doc-host { display: contents; }
	.doc-host.covered { visibility: hidden; }
	.leaf-body.image-canvas, .leaf-body.browser-canvas { overflow: hidden; display: flex; flex-direction: column; }
	.leaf-body.browser-canvas { padding: 0; }
	@media (any-pointer: coarse) and (max-width: 600px) { .leaf-body.document-canvas { padding-inline: 12px; } }

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
	.gone button:hover { border-color: var(--accent); color: var(--violet-text); }
</style>
