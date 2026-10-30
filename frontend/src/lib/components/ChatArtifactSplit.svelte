<script lang="ts">
	import ChatPanel from './ChatPanel.svelte';
	import ArtifactLeaf from './ArtifactLeaf.svelte';
	import ArtifactTabSheet from './ArtifactTabSheet.svelte';
	import CanvasSplitDivider from './CanvasSplitDivider.svelte';
	import { requestArtifactTabClose } from '$lib/artifactTabClose';

	import { pushBackLayer } from '$lib/historyBackLayer';
	import { canvasCompact, artifactTabSheetOpen } from '$lib/stores/artifactCanvas';
	import {
		threadWorkspaces, splitWidth as splitWidthStore, emptyWorkspace, setActiveSurface,
		setActiveTab, setLeafVisible,
	} from '$lib/stores/artifactWorkspace';
	import { computeSplitLayout, ARTIFACT_MIN, DIVIDER_WIDTH, isCompactCanvas } from '$lib/artifactSplitLayout';
	import { documentOpenCapability } from '$lib/artifactDocumentOpenCapability';
	import { visibleDocumentArtifactId } from '$lib/heldDocFeedbackScope';
	import { tabDocumentImage } from '$lib/stores/tabViewState';
	import { returnToPlanNode } from '$lib/planLeafReturn';
	import { releaseDisplayedDocument, updateDocumentOpenCapability } from '$lib/ws/presence';

	let {
		threadId,
		chatPanelRef = $bindable<ChatPanel | undefined>(undefined),
		isDraggingOver,
		onOpenLightbox,
		onAutoCreateThread,
		onOpenDocuments,
		onArtifactHeaderInset = () => {},
	}: {
		threadId: string | null;
		chatPanelRef?: ChatPanel;
		isDraggingOver: boolean;
		onOpenLightbox: (url: string) => void;
		onAutoCreateThread: () => Promise<void>;
		onOpenDocuments?: (documentId?: string) => void;
		onArtifactHeaderInset?: (width: number | null) => void;
	} = $props();

	const workspace = $derived(threadId ? ($threadWorkspaces[threadId] ?? emptyWorkspace()) : emptyWorkspace());
	const hasTabs = $derived(workspace.tabs.length > 0);
	const activeTab = $derived(workspace.tabs.find((tab) => tab.instanceId === workspace.activeTabId) ?? null);

	let splitEl = $state<HTMLDivElement | undefined>(undefined);
	let canvasWidth = $state(0);

	$effect(() => {
		const el = splitEl;
		if (!el) return;
		canvasWidth = el.getBoundingClientRect().width;
		if (typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver((entries) => {
			const entry = entries[0];
			if (entry) canvasWidth = entry.contentRect.width;
		});
		ro.observe(el);
		return () => ro.disconnect();
	});

	const layout = $derived(computeSplitLayout(canvasWidth, hasTabs, workspace.visible, $splitWidthStore));
	// Requests can add the first tab, so retain the measured compact state even
	// while the workspace is still chat-only.
	const compact = $derived(isCompactCanvas(canvasWidth));
	// Compact shows exactly one surface; the artifact takes the canvas only when
	// it is the active surface, so Back always has chat to return to.
	const artifactFullCanvas = $derived(compact && workspace.activeSurface === 'artifact');
	const leafMode = $derived<'rail' | 'expanded' | 'compact' | null>(
		!hasTabs ? null
		: layout.mode === 'split' ? 'expanded'
		: layout.mode === 'rail' ? 'rail'
		: artifactFullCanvas ? 'compact'
		: null
	);
	const artifactWidth = $derived(layout.mode === 'split' ? layout.artifactWidth : ARTIFACT_MIN);
	const artifactHeaderInset = $derived(leafMode === 'expanded' ? artifactWidth + DIVIDER_WIDTH : null);
	// The one document whose header Send is on screen, so the chat does not offer a second sender for it.
	const visibleDocumentId = $derived(visibleDocumentArtifactId({
		expanded: leafMode === 'expanded',
		activeTabKind: activeTab?.kind ?? null,
		artifactId: activeTab?.artifactId ?? null,
		imageOpen: !!(threadId && activeTab && tabDocumentImage(threadId, activeTab.instanceId)),
	}));

	// Header and leaf are siblings. Publish the exact occupied right-column width on
	// their shared ancestor, rather than making either duplicate split layout rules.
	$effect(() => {
		onArtifactHeaderInset(artifactHeaderInset);
		return () => onArtifactHeaderInset(null);
	});

	// Publish the measured mode so the app header can take over in compact.
	$effect(() => { canvasCompact.set(artifactFullCanvas); });

	const openCapability = $derived(documentOpenCapability(workspace, layout, compact));
	// One publisher: a thread switch replaces the published thread in place, and
	// teardown forgets only the thread this view last published.
	let publishedThread: string | null = null;
	$effect(() => {
		if (threadId) updateDocumentOpenCapability(threadId, openCapability);
		else releaseDisplayedDocument(publishedThread);
		publishedThread = threadId;
	});
	$effect(() => () => releaseDisplayedDocument(publishedThread));

	// The sheet only makes sense over a full-canvas artifact.
	$effect(() => { if (!artifactFullCanvas) artifactTabSheetOpen.set(false); });

	// While the artifact owns the canvas it holds a back layer, so hardware Back
	// returns to chat instead of leaving the app. Tabs are never closed by Back.
	$effect(() => {
		if (!artifactFullCanvas || !threadId) return;
		const tid = threadId;
		return pushBackLayer(() => {
			setActiveSurface(tid, 'chat');
			void returnToPlanNode(activeTab?.kind === 'subagent' ? activeTab : null);
			return true;
		});
	});

	function backToChat() {
		if (!threadId) return;
		setActiveSurface(threadId, 'chat');
		void returnToPlanNode(activeTab?.kind === 'subagent' ? activeTab : null);
	}
	function onChatClick() {
		if (threadId) setActiveSurface(threadId, 'chat');
	}
	function onChatKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter' || event.key === ' ') onChatClick();
	}
	function onChatInput() {
		if (threadId) setActiveSurface(threadId, 'chat');
	}
	function onDividerResize(next: number) {
		splitWidthStore.set(next);
	}
	function selectFromSheet(instanceId: string) {
		if (threadId) setActiveTab(threadId, instanceId);
	}
	async function closeFromSheet(instanceId: string) {
		if (!threadId) return;
		const tid = threadId;
		const tab = workspace.tabs.find((t) => t.instanceId === instanceId);
		if (!tab) return;
		await requestArtifactTabClose(tid, tab, () => {
			setActiveTab(tid, instanceId);
			setLeafVisible(tid, true);
			setActiveSurface(tid, 'artifact');
			artifactTabSheetOpen.set(false);
		});
	}
</script>

<div class="split" class:compact bind:this={splitEl}>
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<!-- ChatPanel stays mounted in compact so its scroll position, composer
	     draft, and in-flight turn survive flipping to the artifact and back. -->
	<div class="chat-col" data-drop-default-surface class:offscreen={artifactFullCanvas} onclick={onChatClick} onkeydown={onChatKeydown} oninput={onChatInput}>
		<ChatPanel bind:this={chatPanelRef} {isDraggingOver} {onOpenLightbox} {onAutoCreateThread} {onOpenDocuments} visibleDocumentArtifactId={visibleDocumentId} />
	</div>
	{#if leafMode === 'expanded'}
		<CanvasSplitDivider {artifactWidth} {canvasWidth} onResize={onDividerResize} />
	{/if}
	{#if leafMode}
		<div
			class="leaf-col"
			class:rail={leafMode === 'rail'}
			class:full={leafMode === 'compact'}
			style={leafMode === 'expanded' ? `width:${artifactWidth}px` : ''}
		>
			<ArtifactLeaf
				{threadId}
				mode={leafMode}
				escapeCloses={leafMode === 'compact'}
				onEscapeClose={backToChat}
				{onOpenLightbox}
			/>
		</div>
	{/if}
</div>

{#if $artifactTabSheetOpen && artifactFullCanvas}
	<ArtifactTabSheet
		tabs={workspace.tabs}
		activeKey={workspace.activeTabId}
		onSelect={selectFromSheet}
		onClose={closeFromSheet}
		onCloseSheet={() => artifactTabSheetOpen.set(false)}
	/>
{/if}

<style>
	/* A status banner pushes the split down in flow. The split reaches back up under it, its padding keeping the
	   columns where they are, so the expanded leaf can hold its tab strip in the header band above the banner. */
	.split {
		flex: 1; min-width: 0; min-height: 0; display: flex; overflow: hidden;
		margin-top: calc(-1 * var(--status-banners-height, 0px)); padding-top: var(--status-banners-height, 0px);
	}
	.chat-col { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
	/* Hidden, not unmounted — see the comment on the element. */
	.chat-col.offscreen { display: none; }
	.leaf-col { flex-shrink: 0; min-width: 0; min-height: 0; display: flex; }
	.leaf-col.rail { width: 36px; }
	.leaf-col.full { flex: 1; }
</style>
