<script lang="ts">
	import { fade } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { get } from 'svelte/store';
	import LoginScreen from './LoginScreen.svelte';
	import SetupScreen from './SetupScreen.svelte';
	import { connectOpen } from '$lib/stores/remoteConnect';
	import HomeShell from './HomeShell.svelte';
	import HomeOverlays from './HomeOverlays.svelte';
	import type ChatPanel from './ChatPanel.svelte';
	import type { View } from '$lib/homePaneConfig';
	import type { ProjectPick } from '$lib/stores/projects';
	import { token } from '$lib/stores/auth';
	import { fetchSetupProgress } from '$lib/setupProgress';

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
	// The backend, not this tab, knows whether a signed-in person finished setup. Read once per session, never during setup itself.
	let setupResume = $state(false);
	let setupChecked = false;
	$effect(() => {
		if (!isAuthenticated) { setupChecked = false; return; }
		if (setupMode === 'flow') { setupChecked = true; return; }
		if (setupChecked) return;
		setupChecked = true;
		void fetchSetupProgress(get(token)).then((progress) => {
			// An unreadable record leaves the app as it is: a failed read must never lock out someone who finished setup.
			if (progress && !progress.setup_completed) { setupResume = true; setupMode = 'flow'; }
		});
	});

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
			resume={setupResume}
			onExistingAccount={() => setupMode = 'login'}
			onAccountCreated={() => setupMode = 'flow'}
			onFinished={() => { setupMode = 'login'; setupResume = false; }}
		/>
	</div>
{:else if $connectOpen}
	<div class="auth-gate" out:authGateExit>
		<SetupScreen startConnect onLeaveConnect={() => connectOpen.set(false)} onExistingAccount={() => connectOpen.set(false)} onAccountCreated={() => {}} onFinished={() => connectOpen.set(false)} />
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
