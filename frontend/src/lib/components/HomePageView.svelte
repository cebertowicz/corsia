<script lang="ts">
	import { fade } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import LoginScreen from './LoginScreen.svelte';
	import SetupScreen from './SetupScreen.svelte';
	import HomeShell from './HomeShell.svelte';
	import HomeOverlays from './HomeOverlays.svelte';
	import type ChatPanel from './ChatPanel.svelte';
	import type { View } from '$lib/homePaneConfig';
	import type { ProjectPick } from '$lib/stores/projects';

	let {
		isAuthenticated,
		activeView,
		sidebarOpen = $bindable(false),
		chatPanelRef = $bindable<ChatPanel | undefined>(undefined),
		conductorThreadId,
		conductorKnownAbsent = false,
		systemStatus,
		isDraggingOver,
		paneConfig,
		lightboxUrl = $bindable(''),
		lightboxOpen = $bindable(false),
		deleteConfirmId,
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
		onPickConversation,
		onGoWorkspace,
		onProjectCreated,
		onConfirmDeleteThread,
		onCancelDeleteThread,
	}: {
		isAuthenticated: boolean;
		activeView: View;
		sidebarOpen: boolean;
		chatPanelRef: ChatPanel | undefined;
		conductorThreadId: string | null;
		conductorKnownAbsent?: boolean;
		systemStatus: { memory?: any; cpu?: any; llama_server?: any };
		isDraggingOver: boolean;
		paneConfig: any;
		lightboxUrl: string;
		lightboxOpen: boolean;
		deleteConfirmId: string | null;
		onNewChat: () => void;
		onActivity: () => void;
		onConductor: () => boolean | void | Promise<boolean | void>;
		onDocuments: (lens?: 'all' | 'skills', documentId?: string) => void;
		onAttachProject: (id: string, projectId: string | null, projectName?: string | null) => void | Promise<void>;
		onSelectThread: (id: string) => void | Promise<void>;
		onDeleteThread: (id: string) => void;
		onArchiveThread: (id: string) => void | Promise<void>;
		onOpenSettings: () => void;
		onMeasurement: () => void;
		onCloseMeasurement: () => void;
		onToggleSidebar: () => void;
		onTogglePane: () => void;
		onOpenLightbox: (url: string) => void;
		onAutoCreateThread: () => Promise<void>;
		onPickProject: (project: ProjectPick) => void;
		onPickProjectAndClosePane: (project: ProjectPick) => void;
		onCloseSettings: () => void;
		onPickConversation: (id: string) => void | Promise<void>;
		onGoWorkspace: () => void;
		onProjectCreated: (project: ProjectPick) => void;
		onConfirmDeleteThread: () => void | Promise<void>;
		onCancelDeleteThread: () => void;
	} = $props();

	const easeOutQuint = cubicOut;
	let setupMode = $state<'login' | 'flow'>('login');

	function reduceMotion() {
		return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
	}

	function duration(node: Element, name: '--dur-tight' | '--dur-standard') {
		return Number.parseFloat(getComputedStyle(node).getPropertyValue(name)) || 0;
	}

	function authGateExit(node: Element) {
		return fade(node, { duration: duration(node, reduceMotion() ? '--dur-tight' : '--dur-standard'), easing: easeOutQuint });
	}

	function appGateEnter(node: Element) {
		const reduced = reduceMotion();
		return fade(node, {
			duration: duration(node, reduced ? '--dur-tight' : '--dur-standard'),
			delay: reduced ? 0 : duration(node, '--dur-tight'),
			easing: easeOutQuint,
		});
	}
</script>

{#if setupMode === 'flow'}
	<div class="auth-gate" out:authGateExit>
		<SetupScreen
			onExistingAccount={() => setupMode = 'login'}
			onAccountCreated={() => setupMode = 'flow'}
			onFinished={() => setupMode = 'login'}
		/>
	</div>
{:else if !isAuthenticated}
	<div class="auth-gate" out:authGateExit>
		<LoginScreen onNoAccount={() => setupMode = 'flow'} />
	</div>
{:else}
	<div class="app-gate" in:appGateEnter>
		<HomeShell
		{activeView}
		bind:sidebarOpen
		bind:chatPanelRef
		{conductorThreadId}
		{conductorKnownAbsent}
		{systemStatus}
		{isDraggingOver}
		{paneConfig}
		{onNewChat}
		{onActivity}
			onConductor={onConductor}
		onDocuments={onDocuments}
		onAttachProject={onAttachProject}
		onSelectThread={onSelectThread}
		onDeleteThread={onDeleteThread}
		onArchiveThread={onArchiveThread}
		onOpenSettings={onOpenSettings}
		onToggleSidebar={onToggleSidebar}
		onTogglePane={onTogglePane}
		onOpenLightbox={onOpenLightbox}
		onAutoCreateThread={onAutoCreateThread}
		onPickProject={onPickProject}
		onPickProjectAndClosePane={onPickProjectAndClosePane}
		onCloseSettings={onCloseSettings}
		onMeasurement={onMeasurement}
		onCloseMeasurement={onCloseMeasurement}
		{onGoWorkspace}
	/>
	</div>
{/if}

<HomeOverlays
	bind:lightboxUrl
	bind:lightboxOpen
	{deleteConfirmId}
	onPickProject={onPickProject}
	onPickConversation={onPickConversation}
	onNewChat={onNewChat}
	onGoWorkspace={onGoWorkspace}
	onProjectCreated={onProjectCreated}
	onConfirmDeleteThread={onConfirmDeleteThread}
	onCancelDeleteThread={onCancelDeleteThread}
/>

<style>
	@media (prefers-reduced-motion: reduce) { .auth-gate, .app-gate { animation-duration: var(--dur-tight) !important; } }
</style>
