<script lang="ts">
	import { threads, demoteAgentLocally } from '$lib/stores/threads';
	import {
		reloadThreadPlanStates, stopThreadPlanStates, threadPlanStates, watchThreadPlanStates,
	} from '$lib/stores/threadPlanStates';
	import { onDestroy } from 'svelte';
	import { markThreadSeen } from '$lib/api/plans';
	import { canMarkSeen } from './threadRowMark';
	import { sidebarCollapsed } from '$lib/stores/ui';
	import { token } from '$lib/stores/auth';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import { toasts } from '$lib/stores/toast';
	import { uiScope, exitScope } from '$lib/stores/uiScope';
	import { paletteOpen } from '$lib/stores/palette';
	import { togglePin } from '$lib/stores/pinnedConversations';
	import SidebarThreadList from './SidebarThreadList.svelte';
	import SidebarContextMenu from './SidebarContextMenu.svelte';
	import RailScopedSection from './RailScopedSection.svelte';
	import { measurementRailHref, openMeasurementFromRail } from '$lib/stores/measurementNavigation';
	import type { View } from '$lib/homePaneConfig';
	import NewChatFab from './NewChatFab.svelte';
	import SidebarAttentionList from './SidebarAttentionList.svelte';
	import { projects, loadProjects } from '$lib/stores/projects';
	import { updateStatus } from '$lib/stores/settings';

	const updateAvailable = $derived($updateStatus?.available === true);

	let projectNames = $derived(Object.fromEntries($projects.map((p) => [p.id, p.name])));
	let activeProjects = $derived($projects.filter((p) => p.status === 'active'));

	type ContextMenuKind = 'conversation' | 'agent' | 'conductor';

	let {
		open = $bindable(),
		onSelectThread,
		onDeleteThread,
		onArchiveThread,
		onOpenSettings,
		onMeasurement,
		onNewChat,
		onActivity,
		onConductor,
		conductorThreadId = null,
		conductorKnownAbsent = false,
		onAttachProject,
		activeView = 'chat',
		onGoWorkspace,
	}: {
		open: boolean;
		onSelectThread: (id: string) => void | Promise<void>;
		onDeleteThread: (id: string) => void;
		onArchiveThread?: (id: string) => void;
		onOpenSettings: () => void;
		onMeasurement?: () => void;
		onNewChat?: () => void;
		onActivity?: () => void;
		onConductor?: () => boolean | void | Promise<boolean | void>;
		conductorThreadId?: string | null;
		conductorKnownAbsent?: boolean;
		onAttachProject?: (threadId: string, projectId: string | null, projectName?: string | null) => void;
		activeView?: View;
		onGoWorkspace?: () => void;
	} = $props();

	let contextMenuThreadId = $state<string | null>(null);
	let attention = $state<{ threadId: string; title: string } | null>(null);
	let contextMenuKind = $state<ContextMenuKind>('conversation');
	let contextMenuX = $state(0);
	let contextMenuY = $state(0);

	let projectList = $derived(activeProjects.map((p) => ({ id: p.id, name: p.name, accent_color: p.accent_color })));
	let ctxProjectId = $derived(
		contextMenuThreadId ? ($threads.find((s) => s.id === contextMenuThreadId)?.project_id ?? null) : null
	);

	function backToWorkspace() {
		exitScope();
		onGoWorkspace?.();
	}

	let threadListRef = $state<SidebarThreadList | undefined>(undefined);
	let railScopedRef = $state<RailScopedSection | undefined>(undefined);

	function openContextMenu(e: MouseEvent, id: string, kind: ContextMenuKind = 'conversation') {
		e.preventDefault();
		e.stopPropagation();
		contextMenuThreadId = id;
		contextMenuKind = kind;
		contextMenuX = e.clientX;
		contextMenuY = e.clientY;
	}

	function closeContextMenu() {
		contextMenuThreadId = null;
	}

	const openAttention = (threadId: string, title: string) => (attention = { threadId, title });
	async function selectAttentionThread(threadId: string): Promise<void> {
		if (threadId === conductorThreadId) await onConductor?.();
		else await onSelectThread(threadId);
	}

	function contextMenuRename() {
		const id = contextMenuThreadId;
		const kind = contextMenuKind;
		closeContextMenu();
		if (!id) return;
		if (kind === 'agent') railScopedRef?.startRename(id);
		else threadListRef?.startRename(id);
	}

	// Seen state is the server's, so a mark reaches every device through its nudge.
	async function contextMenuToggleUnread() {
		const id = contextMenuThreadId;
		const unread = id ? Boolean($threadPlanStates[id]?.has_update) : false;
		closeContextMenu();
		if (!id || !$token) return;
		if (await markThreadSeen($token, id, !unread)) reloadThreadPlanStates([id]);
		else toasts.show(unread ? "Couldn't mark read" : "Couldn't mark unread", 'error');
	}

	async function contextMenuAgentSettings() {
		const id = contextMenuThreadId;
		closeContextMenu();
		if (!id) return;
		await onSelectThread(id);
		threadSettingsOpen.set(true);
	}

	async function contextMenuDeactivateAgent() {
		const id = contextMenuThreadId;
		const authToken = $token;
		closeContextMenu();
		if (!id || !authToken) return;
		try {
			const query = new URLSearchParams({ token: authToken });
			const response = await fetch(`/api/agents/${id}?${query}`, { method: 'DELETE' });
			if (!response.ok) {
				const body = await response.json().catch(() => ({})) as { detail?: string };
				throw new Error(body.detail ?? `Deactivate failed (${response.status})`);
			}
			demoteAgentLocally(id);
			toasts.show('Agent deactivated', 'success');
		} catch (error) {
			toasts.show(error instanceof Error ? error.message : "Couldn't deactivate Agent", 'error');
		}
	}

	function contextMenuArchive() {
		const id = contextMenuThreadId;
		closeContextMenu();
		if (!id) return;
		onArchiveThread?.(id);
	}

	function contextMenuDelete() {
		const id = contextMenuThreadId;
		closeContextMenu();
		if (!id) return;
		onDeleteThread(id);
	}

	function contextMenuPin() {
		const id = contextMenuThreadId;
		closeContextMenu();
		if (!id) return;
		togglePin(id);
	}

	function contextMenuMoveToProject(projectId: string | null) {
		const id = contextMenuThreadId;
		closeContextMenu();
		if (!id) return;
		onAttachProject?.(id, projectId, projectId ? (projectNames[projectId] ?? null) : null);
	}

	$effect(() => {
		if ($token) {
			loadProjects();
		}
	});

	// Both lists show each listed thread's plan state (#2220); one reading serves them.
	$effect(() => {
		const ids = $threads
			.filter((s) => ($uiScope ? s.project_id === $uiScope.id : !s.project_id))
			.map((s) => s.id);
		watchThreadPlanStates(conductorThreadId ? [conductorThreadId, ...ids] : ids);
	});
	onDestroy(stopThreadPlanStates);
</script>

<aside
	class="sidebar"
	class:open
	class:collapsed={$sidebarCollapsed}
>
	<div class="shell-top-pad" data-tauri-drag-region></div>

	<nav class="rail-nav" aria-label="Global">
		<button
			class="rail-link press-tint"
			class:active={activeView === 'activity'}
			onclick={() => onActivity?.()}
			title="Activity"
			aria-current={activeView === 'activity' ? 'page' : undefined}
		>
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16"/><path d="M4 12h10"/><path d="M4 18h13"/></svg>
			<span>Activity</span>
		</button>
		<a class="rail-link press-tint" class:active={activeView === 'measurement'} href={measurementRailHref($uiScope?.id ?? null)} onclick={(event) => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return; event.preventDefault(); openMeasurementFromRail($uiScope?.id ?? null); onMeasurement?.(); }} title="Measurement" aria-current={activeView === 'measurement' ? 'page' : undefined}><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18M7 16l4-5 3 3 4-7"/></svg><span>Measurement</span></a>
		<button class="rail-link press-tint" onclick={() => paletteOpen.set(true)} title="Jump to a project, conversation, or doc">
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
			<span>Jump to…</span>
			<kbd class="rail-kbd">⌘K</kbd>
		</button>
		<button class="rail-link press-tint" onclick={() => onOpenSettings?.()} title="Settings">
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>
			<span>Settings</span>
			{#if updateAvailable}
				<svg class="update-icon" role="img" aria-label="Update available" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/></svg>
			{/if}
		</button>
	</nav>

	<div class="sidebar-scroll">
		<div class="rail-scope">
			{#if $uiScope}
				<button class="rail-scope-btn" onclick={backToWorkspace} title="Back to workspace">
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
					<span>Back to workspace</span>
				</button>
			{:else}
				<div class="rail-scope-here">
					<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>
					<span>Workspace</span>
				</div>
			{/if}
		</div>

		<RailScopedSection
			bind:this={railScopedRef}
			{onConductor}
			{onSelectThread}
			{conductorThreadId}
			{conductorKnownAbsent}
			{activeView}
			onOpenAttention={openAttention}
			onOpenContextMenu={openContextMenu}
		/>

		<SidebarThreadList
			bind:this={threadListRef}
			{onSelectThread}
			{onDeleteThread}
			{onArchiveThread}
			onOpenAttention={openAttention}
			onOpenContextMenu={openContextMenu}
		/>
	</div>

</aside>

<SidebarAttentionList bind:attention onSelect={selectAttentionThread} />
<NewChatFab activeProjects={activeProjects} onNewChat={onNewChat} sidebarOpen={open} />

{#if contextMenuThreadId}
	<SidebarContextMenu
		threadId={contextMenuThreadId}
		x={contextMenuX}
		y={contextMenuY}
		projects={projectList}
		currentProjectId={ctxProjectId}
		kind={contextMenuKind}
		isUnread={Boolean($threadPlanStates[contextMenuThreadId]?.has_update)}
		canMarkSeen={canMarkSeen($threadPlanStates[contextMenuThreadId])}
		onToggleUnread={contextMenuToggleUnread}
		onPin={contextMenuPin}
		onRename={contextMenuRename}
		onAgentSettings={contextMenuAgentSettings}
		onMoveToProject={contextMenuMoveToProject}
		onArchive={contextMenuArchive}
		onDeactivateAgent={contextMenuDeactivateAgent}
		onDelete={contextMenuDelete}
		onClose={closeContextMenu}
	/>
{/if}

<style>
	.sidebar {
		width: var(--sidebar-width);
		position: relative;
		background: transparent;
		border-right: 1px solid var(--border-subtle);
		display: flex; flex-direction: column; flex-shrink: 0;
		z-index: 0; overflow: hidden;
		transition: width var(--dur-spring) var(--ease-material);
		-webkit-user-select: none;
		user-select: none;
		-webkit-touch-callout: none;
	}
	.sidebar::before {
		content: '';
		position: absolute;
		inset: 0;
		z-index: -1;
		pointer-events: none;
		background: var(--glass-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-rim);
		transform: translate3d(0, 0, 0);
	}
	:global(html[data-theme="light"]) .sidebar::before {
		backdrop-filter: blur(40px) saturate(100%);
		-webkit-backdrop-filter: blur(40px) saturate(100%);
	}
	.sidebar :global(input),
	.sidebar :global(textarea) {
		-webkit-user-select: text;
		user-select: text;
	}
	.sidebar.collapsed { width: 0; border-right: none; }
	.shell-top-pad {
		flex-shrink: 0;
		min-width: var(--sidebar-width);
		height: env(titlebar-area-height, 0px);
		-webkit-app-region: drag;
		app-region: drag;
	}
	/* The externally served Tauri shell has no titlebar-area environment
	   variable. Native code owns this state so normal windows clear traffic
	   lights while native fullscreen fills the viewport. */
	:global(html[data-shell="tauri"]) .shell-top-pad { height: 52px; }
	:global(html[data-native-fullscreen="true"]) .shell-top-pad,
	:global(html:-webkit-full-screen) .shell-top-pad,
	:global(html:fullscreen) .shell-top-pad {
		height: 0;
	}

	.rail-nav {
		display: flex; flex-direction: column; gap: 1px; flex-shrink: 0;
		padding: 8px 8px 4px;
	}
	.sidebar-scroll {
		flex: 1; min-width: 0; min-height: 0;
		overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain;
	}
	.rail-link {
		display: flex; align-items: center; gap: 10px;
		padding: 7px 10px; border-radius: var(--radius-sm, 8px);
		background: none; border: none; cursor: pointer; width: 100%; text-decoration: none;
		font-size: var(--text-subhead); color: var(--text-secondary); text-align: left;
		transition: background var(--dur-tight) ease, color var(--dur-tight) ease;
	}
	.rail-link:hover { background: var(--bg-hover); color: var(--text-primary); }
	.rail-link.active { background: var(--bg-hover); color: var(--text-primary); font-weight: var(--fw-medium); }
	.rail-link svg { flex-shrink: 0; opacity: 0.85; }
	.rail-link span { flex: 1; }
	.rail-link .update-icon {
		flex: 0 0 auto;
		/* Full opacity against the 0.85 leading icons: reads as "attention"
		   through contrast and shape, with no accent colour to clash with a
		   project's own accent. */
		opacity: 1;
	}
	.rail-kbd {
		font-family: var(--font-mono, 'SF Mono', monospace); font-size: var(--text-caption);
		color: var(--text-tertiary);
	}
	.rail-scope {
		padding: 12px 14px 4px 18px; margin-top: 4px;
	}
	.rail-scope-btn, .rail-scope-here {
		display: flex; align-items: center; gap: 13px;
		background: none; border: none; padding: 2px 0;
		font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary);
	}
	.rail-scope-btn { cursor: pointer; }
	.rail-scope-btn:hover { color: var(--text-secondary); }
	.rail-scope-here svg { color: var(--accent); opacity: 0.9; }

	@media (max-width: 768px) {
		.sidebar {
			position: absolute; top: 0; left: 0; bottom: 0;
			width: min(320px, 85vw) !important;
			z-index: 0;
		}
		.sidebar.collapsed { width: min(320px, 85vw) !important; }
		:global(html[data-theme="light"]) .sidebar::before {
			background: color-mix(in srgb, var(--bg-secondary) 75%, transparent);
		}
		.shell-top-pad {
			height: var(--safe-top);
		}
	}
</style>
