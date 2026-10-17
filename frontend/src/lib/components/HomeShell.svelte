<script lang="ts">
	import { fade } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { activeThread, activeThreadId } from '$lib/stores/threads';
	import { uiScope } from '$lib/stores/uiScope';

	import { paneOpen } from '$lib/stores/paneOpen';
	import { paneDragging } from '$lib/stores/paneOpen';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import { createMainDrawerController } from '$lib/mainDrawerController';
	import { get } from 'svelte/store';
	import {
		threadWorkspaces, emptyWorkspace, openSubagentConversationTab,
		openLegacyDiscussionArchiveTab,
	} from '$lib/stores/artifactWorkspace';
	import { docPickerTarget } from '$lib/stores/docPickerTarget';
	import { toasts } from '$lib/stores/toast';
	import { toastAnchorSurface } from '$lib/stores/toastAnchor';

	import Sidebar from './Sidebar.svelte';
	import Header from './Header.svelte';
	import StatusBanners from './StatusBanners.svelte';
	import ThreadSettingsView from './ThreadSettingsView.svelte';
	import ChatPanel from './ChatPanel.svelte';
	import ChatArtifactSplit from './ChatArtifactSplit.svelte';
	import SettingsView from './SettingsView.svelte';
	import MeasurementPage from './MeasurementPage.svelte';
	import DocumentsView from './DocumentsView.svelte';
	import ActivityView from './activity/ActivityView.svelte';
	import Pane from './Pane.svelte';
	// Document comments render beside their anchored text in DocViewer.
	import DocDetailPane from './DocDetailPane.svelte';

	import type { PaneConfig, View } from '$lib/homePaneConfig';
	import type { ProjectPick } from '$lib/stores/projects';


	let {
		activeView,
		sidebarOpen = $bindable(false),
		chatPanelRef = $bindable<ChatPanel | undefined>(undefined),
		conductorThreadId,
		conductorKnownAbsent = false,
		systemStatus,
		isDraggingOver,
		paneConfig,
		onNewChat,
		onActivity,
		onConductor,
		onDocuments,
		onAttachProject,
		onSelectThread,
		onDeleteThread,
		onArchiveThread,
		onOpenSettings,
		onToggleSidebar,
		onTogglePane,
		onOpenLightbox,
		onAutoCreateThread,
		onPickProject,
		onPickProjectAndClosePane,
		onCloseSettings,
		onMeasurement,
		onCloseMeasurement,
		onGoWorkspace,
	}: {
		activeView: View;
		sidebarOpen: boolean;
		chatPanelRef?: ChatPanel;
		conductorThreadId: string | null;
		conductorKnownAbsent?: boolean;
		systemStatus: { memory?: any; cpu?: any; llama_server?: any };
		isDraggingOver: boolean;
		paneConfig: PaneConfig | null;
		onNewChat: () => void;
		onActivity: () => void;
		onConductor: () => boolean | void | Promise<boolean | void>;
		onDocuments: (lens?: 'all' | 'skills', documentId?: string) => void;
		onAttachProject: (threadId: string, projectId: string | null, projectName?: string | null) => void;
		onSelectThread: (id: string) => void | Promise<void>;
		onDeleteThread: (id: string) => void;
		onArchiveThread: (id: string) => void;
		onOpenSettings: () => void;
		onToggleSidebar: () => void;
		onTogglePane: () => void;
		onOpenLightbox: (url: string) => void;
		onAutoCreateThread: () => Promise<void>;
		onPickProject: (project: ProjectPick) => void;
		onPickProjectAndClosePane: (project: ProjectPick) => void;
		onCloseSettings: () => void;
		/** PL-1/PL-2: the rail, header and Activity doorways open this surface. */
		onMeasurement: () => void;
		/** Close returns to the doorway that opened it, else to Activity. */
		onCloseMeasurement: () => void;
		onGoWorkspace: () => void;
	} = $props();


	const easeOutQuint = cubicOut;
	let mainEl = $state<HTMLElement | undefined>(undefined);
	let artifactHeaderInset = $state<number | null>(null);
	const mainDrawer = createMainDrawerController({
		getMainEl: () => mainEl,
		getSidebarOpen: () => sidebarOpen,
		setSidebarOpen: (open) => { sidebarOpen = open; },
		getPaneOpen: () => get(paneOpen) && viewHasPane,
		setPaneOpen: (open) => paneOpen.set(open),
		setPaneDragging: (dragging) => paneDragging.set(dragging),
	});
	const workspace = $derived($activeThreadId ? ($threadWorkspaces[$activeThreadId] ?? emptyWorkspace()) : emptyWorkspace());
	// Only a composed chat surface with a visible leaf may protect split minima.
	const protectArtifactSplit = $derived(activeView === 'chat' && workspace.tabs.length > 0 && workspace.visible);
	// The configured pane stays visible while document comments open in-canvas.
	const paneView = $derived(activeView === 'documents' ? 'detail' : 'config');
	// Views without a pane (project Activity, Measurement, Settings) hide the toggle.
	const viewHasPane = $derived(paneView === 'detail' || paneConfig != null);
	const paneVisible = $derived($paneOpen && viewHasPane);

	function browseDocumentsForThread(threadId: string) {
		docPickerTarget.set({ threadId });
		paneOpen.set(false);
		onDocuments('all');
	}

	$effect(() => {
		const el = mainEl;
		if (!el) return;
		return mainDrawer.attachTouchMove(el);
	});

	// A drawer state change always drops any leftover drag transform.
	$effect(() => {
		void sidebarOpen;
		void $paneOpen;
		mainDrawer.reset();
	});

	function openDocumentsForContextAttach() {
		onDocuments('all');
	}

	function openDocumentsForSkills() {
		onDocuments('skills');
	}

	function openDocumentsFromThreadSettings() {
		threadSettingsOpen.set(false);
		openDocumentsForContextAttach();
	}

	function openSkillDocumentsFromThreadSettings() {
		threadSettingsOpen.set(false);
		openDocumentsForSkills();
	}

	async function openActivityChild(
		parentThreadId: string,
		nodeId: string | null,
		childThreadId: string,
		title: string,
		stepId?: string,
	) {
		await onSelectThread(parentThreadId);
		openSubagentConversationTab(parentThreadId, {
			threadId: childThreadId,
			title,
			// A rolled subagent has no plan step, so no return point to remember.
			sourceNodeId: nodeId ?? undefined,
			sourceStepId: stepId,
		});
	}

	async function openActivityDiscussion(
		parentThreadId: string,
		artifactId: string,
		title: string,
	) {
		await onSelectThread(parentThreadId);
		openLegacyDiscussionArchiveTab(parentThreadId, { artifactId, title });
	}

	function attachmentInputForDrop(target: EventTarget | null, app: HTMLElement): HTMLInputElement | null {
		const subagentSurface = target instanceof Element
			? target.closest('[data-drop-composer-surface]')
			: null;
		return subagentSurface?.querySelector<HTMLInputElement>('[data-composer-attachment-input]')
			?? app.querySelector<HTMLInputElement>('[data-drop-default-surface] [data-composer-attachment-input]')
			?? null;
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="app"
	in:fade={{ duration: 460, delay: 200, easing: easeOutQuint }}
	ondragover={(e) => { e.preventDefault(); isDraggingOver = true; }}
	ondragleave={(e) => {
		if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) isDraggingOver = false;
	}}
	ondrop={(e) => {
		e.preventDefault();
		isDraggingOver = false;
		const files = e.dataTransfer?.files;
		const input = attachmentInputForDrop(e.target, e.currentTarget as HTMLElement);
		if (files && files.length > 0) {
			if (!input) {
				toasts.show('Open a chat to add files', 'info');
				return;
			}
			input.files = files;
			input.dispatchEvent(new Event('change', { bubbles: true }));
		}
	}}
>
	<Sidebar
		bind:open={sidebarOpen}
		onNewChat={onNewChat}
		onActivity={onActivity}
			onConductor={onConductor}
		{conductorThreadId}
		{conductorKnownAbsent}
		onAttachProject={onAttachProject}
		onSelectThread={onSelectThread}
		onDeleteThread={onDeleteThread}
		onArchiveThread={onArchiveThread}
		onOpenSettings={onOpenSettings}
		onMeasurement={onMeasurement}
		{activeView}
		{onGoWorkspace}
	/>

	<main
		class="main"
		class:open={sidebarOpen}
		class:pane-open={paneVisible}
		style:--artifact-header-inset={artifactHeaderInset === null ? undefined : `${artifactHeaderInset}px`}
		bind:this={mainEl}
		use:toastAnchorSurface
		ontransitionstart={mainDrawer.onTransitionStart}
		ontransitionend={mainDrawer.onTransitionEnd}
		ontouchstart={mainDrawer.onTouchStart}
		ontouchend={mainDrawer.onTouchEnd}
		ontouchcancel={mainDrawer.onTouchEnd}
	>
		<Header onToggleSidebar={onToggleSidebar} onTogglePane={onTogglePane} {onMeasurement} {activeView} {conductorThreadId} hasPane={viewHasPane} />
		<StatusBanners />
		{#if $threadSettingsOpen && ($activeThread || (conductorThreadId != null && $activeThreadId === conductorThreadId))}
			<ThreadSettingsView
				conductor={conductorThreadId != null && $activeThreadId === conductorThreadId}
				onBrowseContextFiles={openDocumentsFromThreadSettings}
				onBrowseSkills={openSkillDocumentsFromThreadSettings}
			/>
		{:else if activeView === 'activity'}
			<ActivityView
				projectId={$uiScope?.id ?? null}
				scopeName={$uiScope?.name ?? 'Workspace'}
				onMeasurement={onMeasurement}
				onSelectThread={onSelectThread}
				onOpenChild={openActivityChild}
				onOpenLegacyDiscussion={openActivityDiscussion}
					{onGoWorkspace}
			/>
		{:else if activeView === 'chat'}
			<ChatArtifactSplit
				bind:chatPanelRef
				threadId={$activeThreadId}
				{isDraggingOver}
				onOpenLightbox={onOpenLightbox}
				onAutoCreateThread={onAutoCreateThread}
				onOpenDocuments={(documentId) => onDocuments('all', documentId)}
				onArtifactHeaderInset={(width) => (artifactHeaderInset = width)}
			/>
		{:else if activeView === 'settings'}
			<SettingsView
				onClose={onCloseSettings}
				{systemStatus}
				onBrowseContextFiles={openDocumentsForContextAttach}
				onBrowseSkills={openDocumentsForSkills}
			/>
		{:else if activeView === 'measurement'}
			<div class="measurement-scroller"><MeasurementPage onClose={onCloseMeasurement} /></div>
		{:else if activeView === 'documents'}
			<div class="docs-scroller"><DocumentsView /></div>
		{/if}
		<button
			class="main-close-tap"
			class:open={sidebarOpen || paneVisible}
			onclick={() => { sidebarOpen = false; paneOpen.set(false); }}
			aria-label="Close drawer"
			aria-hidden={!(sidebarOpen || paneVisible)}
			tabindex={sidebarOpen || paneVisible ? 0 : -1}
		></button>
	</main>

	{#if paneView === 'detail'}
		<DocDetailPane />
	{:else if paneConfig}
		<Pane
			label={paneConfig.label}
			sections={paneConfig.sections}
			{protectArtifactSplit}
			onPickProject={onPickProjectAndClosePane}
			onBrowseDocuments={browseDocumentsForThread}
		/>
	{/if}
</div>

<style>
	.app { position: fixed; inset: 0; display: flex; overflow: hidden; background: transparent; }
	@media (max-width: 1024px) {
		.main { transition: transform var(--dur-spring) var(--ease-entrance); }
		.main.pane-open { transform: translateX(calc(-1 * min(420px, 92vw))); }
		.main-close-tap.open { display: block; pointer-events: auto; }
	}
	.main {
		flex: 1; display: flex; flex-direction: column; min-width: 0;
		position: relative;
		background: var(--bg-primary);
		z-index: 10;
	}
	.main:has(:global([data-popover])) { z-index: 12; }
	.main-close-tap {
		display: none; position: absolute; inset: var(--header-bottom) 0 0; z-index: 9;
		background: transparent; border: none; cursor: default;
		opacity: 0; pointer-events: none;
		-webkit-tap-highlight-color: transparent;
	}
	.docs-scroller {
		flex: 1; min-height: 0; overflow-y: auto;
		padding: calc(var(--header-bottom) + 8px) 20px 0;
	}
	.measurement-scroller {
		flex: 1; min-height: 0; overflow-y: auto;
		padding: calc(var(--header-bottom) + 20px) 20px 32px;
	}
	@media (max-width: 768px) {
		/* The phone header is a translucent overlay, so a scroller that starts under it shows section titles through it. Start the scroller below the header instead. */
		.measurement-scroller { margin-top: var(--header-bottom); padding: 14px 14px 24px; }
	}
	:global(.spin) { animation: spin 1s linear infinite; }
	@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
	@media (max-width: 768px) {
		.app { bottom: auto; height: 100vh; }
		@media (display-mode: standalone) { .app { height: 100dvh; } }
		.main {
			transform: translateX(0);
			transition: transform var(--dur-spring) var(--ease-entrance);
			will-change: transform;
			border-top-left-radius: 32px;
			border-bottom-left-radius: 32px;
			overflow: hidden;
			box-shadow:
				-12px 0 36px rgba(0, 0, 0, 0.28),
				-1px 0 0 rgba(255, 255, 255, 0.05);
		}
		.main.open { transform: translateX(min(320px, 85vw)); }
		.main-close-tap.open { display: block; pointer-events: auto; }
	}
</style>
