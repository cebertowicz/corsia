<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { get } from 'svelte/store';
	import { token, user, isAuthenticated } from '$lib/stores/auth';
	import { threads, activeThreadId, activeThread, messages, reasoningEffort, clearThreadBuffers, saveThreadProject, adoptCreatedThread } from '$lib/stores/threads';
	import { toasts } from '$lib/stores/toast';
	import { activeModelId, loadModels, getLastChosenModelId, reconcileThreadModelSnapshots } from '$lib/stores/models';
	import { loadSettings, loadUpdateStatus } from '$lib/stores/settings';
	import { connect, on, subscribeThread } from '$lib/ws';
	import { sidebarCollapsed } from '$lib/stores/ui';
	import { isMobile } from '$lib/stores/viewport';
	import { checkSecurityAlert } from '$lib/stores/connection';
	import { uiScope, exitScope } from '$lib/stores/uiScope';
	import { enterProjectScope, enterThreadScope } from '$lib/stores/projects';
	import { paneOpen } from '$lib/stores/paneOpen';
	import { paneCollapsed } from '$lib/stores/paneCollapsed';
	import { watchSubagentOpenRequests } from '$lib/subagentOpen';
	import { openThread } from '$lib/stores/openThread';
	import { threadSettingsOpen } from '$lib/stores/threadSettings';
	import { contextAttachTarget } from '$lib/stores/contextAttach';
	import { closeSettingsBack, recordSettingsHomeThread, requestSettingsSection, settingsNavigation } from '$lib/stores/settingsNavigation';
	import { closeMeasurementSurface, measurementViewRequested } from '$lib/measurementSurfaceNavigation';
	import type { Thread } from '$lib/stores/threads';
	import { fetchScopeTarget, landOnFirstRun, type CoordinationLookup } from '$lib/utils/conductor';
	import type ChatPanel from '$lib/components/ChatPanel.svelte';
	import HomePageView from '$lib/components/HomePageView.svelte';
	import { docLens, selectedDocId } from '$lib/stores/documentsSurface';
	import { emitProductEvent } from '$lib/product_events';
	import { registerHomeLifecycle } from '$lib/homeLifecycle';
	import { createHomeThreadSelector } from '$lib/homeThreadSelection';
	import { buildPaneConfig, type View } from '$lib/homePaneConfig';
	import { syncCanvasHeader } from '$lib/homeCanvasHeader';
	import { buildNewThreadBody } from '$lib/threadCreate';
	import { clearDocPickerTarget, docPickerTarget } from '$lib/stores/docPickerTarget';
	import { showArtifactThreadRequest } from '$lib/openArtifactInThread';
	import { canvasCompact, artifactTabSheetOpen } from '$lib/stores/artifactCanvas';
	import {
		threadWorkspaces, emptyWorkspace, getActiveTab, hasChangedTab, setActiveSurface,
	} from '$lib/stores/artifactWorkspace';
	import { checkThreadRevisions, startDocumentRealtime } from '$lib/documentRealtime';
	import { returnToPlanNode } from '$lib/planLeafReturn';
	import {
		getTurnState,
		resetTurnState,
		restoreTurnState,
	} from '$lib/ws/turnState';
	import { cancelThreadTypewriters } from '$lib/ws/typewriter';

	let activeView = $state<View>(measurementViewRequested() ? 'measurement' : 'activity');
	let sidebarOpen = $state(false);
	let conductorLookup = $state<CoordinationLookup>({ kind: 'unknown' });
	const conductorThreadId = $derived(conductorLookup.kind === 'target' ? conductorLookup.target.thread_id : null);
	const conductorKnownAbsent = $derived(conductorLookup.kind === 'absent');

	let lastEmittedView: View = 'activity';
	$effect(() => {
		const next = activeView;
		if (next === lastEmittedView) return;
		emitProductEvent('ui', 'tab_switched', { from: lastEmittedView, to: next });
		lastEmittedView = next;
	});

	let lastSettingsOpenRequest = 0;
	$effect(() => {
		const request = $settingsNavigation.openRequest;
		if (request === lastSettingsOpenRequest) return;
		lastSettingsOpenRequest = request;
		openSettings();
	});

	let isDraggingOver = $state(false);
	let lightboxUrl = $state('');
	let lightboxOpen = $state(false);

	let systemStatus = $state<{ memory?: any; cpu?: any; llama_server?: any }>({});
	let deleteConfirmId = $state<string | null>(null);
	let chatPanelRef = $state<ChatPanel | undefined>(undefined);
	// Each deliberate selection advances this route-local intent. A first-run
	// opener holds one generation and must yield as soon as the user chooses.
	let selectionIntent = 0;

	async function pollSystem() {
		if (!$token) return;
		try {
			const res = await fetch(`/api/system?token=${$token}`);
			systemStatus = await res.json();
		} catch { /* ignore */ }
	}

	function resetSurface(
		view: View,
		options: { preserveActiveThread?: boolean } = {},
	) {
		// Deliberate surface navigation owns the empty state just as a thread
		// selection does; a pending first-run opener must not repopulate it.
		selectionIntent++;
		openThread.set(null);
		if (view !== 'documents') {
			contextAttachTarget.set(null);
			// A picker owns its originating thread only while the Documents route is
			// active. Any other navigation must not leak that target to a later open.
			clearDocPickerTarget();
		}
		threadSettingsOpen.set(false);
		// Settings keeps the thread it opened over, so a file opened there has a tab to land in.
		recordSettingsHomeThread(view, get(activeThreadId));
		if (!options.preserveActiveThread) activeThreadId.set(null);
		activeView = view;
		if (!options.preserveActiveThread) messages.set([]);
		sidebarOpen = false;
		paneOpen.set(false);
	}

	function navigateHome() {
		exitScope();
		resetSurface('activity');
	}

	function openActivity() {
		resetSurface('activity');
	}

	async function goConductor(): Promise<boolean> {
		sidebarOpen = false;
		paneOpen.set(false);
		conductorLookup = await fetchScopeTarget($token, $uiScope);
		if (conductorLookup.kind === 'target') {
			const target = conductorLookup.target;
			if ('reasoning_effort' in target) reasoningEffort.set(target.reasoning_effort ?? null);
			await selectThread(target.thread_id);
			return true;
		}
		// A user with no conductor has nothing to land on, so the chat surface stays.
		if (conductorLookup.kind === 'absent') { resetSurface('chat'); return true; }
		// Unresolved lookup: keep the surface, leave the rail retryable.
		toasts.show(`Couldn't load ${$uiScope ? 'the conductor' : 'the CORSIA agent'} — try again`, 'error');
		return false;
	}
	$effect(() => {
		const [t, scope, authenticated] = [$token, $uiScope, $isAuthenticated];
		conductorLookup = { kind: 'unknown' };
		if (!authenticated) return;
		let active = true;
		const firstRunIntent = selectionIntent;
		void fetchScopeTarget(t, scope).then((lookup) => {
			if (!active) return;
			conductorLookup = lookup;
			const ownsFirstRunSelection = () =>
				active && $isAuthenticated && $token === t && $uiScope === scope && selectionIntent === firstRunIntent;
			landOnFirstRun(lookup, scope, (id) => (activeView === 'measurement' ? undefined : selectThread(id, 'other', ownsFirstRunSelection))); // a link naming Measurement keeps it
		});
		return () => { active = false; };
	});

	function goDocuments(lens: 'all' | 'skills' = 'all', documentId?: string) {
		const attachTarget = get(contextAttachTarget);
		const picker = get(docPickerTarget);
		docLens.set(lens);
		selectedDocId.set(documentId ?? null);
		// A document picked *for* a thread must return to that thread, so its
		// thread survives the trip to the library.
		resetSurface('documents', {
			preserveActiveThread: attachTarget?.returnTo === 'thread-settings' || !!picker || !!documentId,
		});
		if (picker) {
			activeThreadId.set(picker.threadId);
			return;
		}
		if (attachTarget?.returnTo === 'thread-settings' && attachTarget.threadId) {
			activeThreadId.set(attachTarget.threadId);
		}
	}

	function toggleSidebar() {
		if (typeof window !== 'undefined' && window.innerWidth <= 768) {
			if (!sidebarOpen) paneOpen.set(false);
			sidebarOpen = !sidebarOpen;
		} else {
			$sidebarCollapsed = !$sidebarCollapsed;
		}
	}
	function togglePane() {
		if (typeof window !== 'undefined' && window.innerWidth <= 1024) {
			if (!get(paneOpen)) sidebarOpen = false;
			paneOpen.update((v) => !v);
		} else {
			paneCollapsed.update((v) => !v);
		}
	}

	async function attachThreadToProject(id: string, projectId: string | null, projectName?: string | null) {
		try {
			await saveThreadProject(id, projectId, $token);
			toasts.show(projectId ? `Moved to ${projectName ?? 'project'}` : 'Removed from project', 'success');
			// The active conversation's scope follows its project assignment.
			if (id === $activeThreadId) {
				const moved = $threads.find(s => s.id === id);
				if (moved) await enterThreadScope(moved);
			}
		} catch {
			toasts.show("Couldn't move the conversation — try again", 'error');
		}
	}

	const paneConfig = $derived(
		buildPaneConfig(activeView, $activeThread, $uiScope, conductorThreadId, $activeThreadId)
	);

	function pickProject(p: { id: string; name: string; accent_color?: string | null }) { enterProjectScope(p); resetSurface('activity'); }

	function pickProjectAndClosePane(p: { id: string; name: string; accent_color?: string | null }) {
		pickProject(p);
		paneOpen.set(false);
	}

	function openSettings() {
		emitProductEvent('ui', 'settings_opened', {});
		resetSurface('settings');
		loadSettings();
	}

	function openLightbox(url: string) {
		lightboxUrl = url;
		lightboxOpen = true;
		emitProductEvent('ui', 'modal_opened', { modal_name: 'lightbox' });
	}

	function closeThreadSettings() {
		threadSettingsOpen.set(false);
		if (activeView === 'documents') {
			activeView = 'chat';
			paneOpen.set(false);
		}
	}

	function closeDocumentsView() {
		const target = get(contextAttachTarget);
		contextAttachTarget.set(null);
		// Leaving the library cancels a pending pick rather than letting it apply
		// to whatever is opened next.
		clearDocPickerTarget();
		paneOpen.set(false);
		if (target?.returnTo === 'thread-settings') {
			activeView = 'chat';
			if (target.threadId) activeThreadId.set(target.threadId);
			threadSettingsOpen.set(true);
			return;
		}
		if (target?.returnTo === 'settings') {
			requestSettingsSection(target.returnSection ?? 'context');
			return;
		}
		activeView = 'activity';
	}

	const activeWorkspace = $derived(
		$activeThreadId ? ($threadWorkspaces[$activeThreadId] ?? emptyWorkspace()) : emptyWorkspace()
	);

	$effect(() => {
		const tab = $canvasCompact ? getActiveTab(activeWorkspace) : null;
		syncCanvasHeader({
			threadSettingsOpen: $threadSettingsOpen,
			settingsDrill: $settingsNavigation.drill,
			compactArtifact: tab && $activeThreadId
				? {
					title: tab.title,
					tabCount: activeWorkspace.tabs.length,
					anyChanged: hasChangedTab(activeWorkspace),
					onBack: () => {
						// Back precedence: the sheet closes first; otherwise the
						// artifact hands the canvas back to chat. Tabs are kept.
						if (get(artifactTabSheetOpen)) { artifactTabSheetOpen.set(false); return; }
						setActiveSurface($activeThreadId!, 'chat');
						void returnToPlanNode(tab.kind === 'subagent' ? tab : null);
						tick().then(() => chatPanelRef?.scrollToBottom());
					},
					onTabs: () => artifactTabSheetOpen.update((v) => !v),
				}
				: null,
			contextAttachTarget: $contextAttachTarget,
			activeView,
			uiScopeName: $uiScope?.name ?? null,
			activeThreadId: $activeThreadId, conductorThreadId,
			threads: $threads,
			closeThreadSettings,
			closeSettings: () => closeSettingsBack((id) => selectThread(id, 'other'), openActivity),
			closeDocuments: closeDocumentsView,
		});
	});

	// Opening a document from the library or Settings returns to the thread that
	// now owns the tab, so the new tab is actually on screen.
	$effect(() => {
		const request = $showArtifactThreadRequest;
		if (!request) return;
		showArtifactThreadRequest.set(null);
		if (request.threadId !== get(activeThreadId)) {
			void selectThread(request.threadId, 'other');
			return;
		}
		contextAttachTarget.set(null);
		threadSettingsOpen.set(false);
		activeView = 'chat';
		paneOpen.set(false);
	});

	// The websocket nudge carries no revision and also fires on reads, so it only
	// triggers a read-only re-check; focus covers changes made while away.
	$effect(() => startDocumentRealtime());
	$effect(() => { checkThreadRevisions($activeThreadId); });

	$effect(() => watchSubagentOpenRequests({ selectThread: (id) => selectThread(id, 'link') }));

	async function newThread(projectId?: string | null) {
		selectionIntent++;
		const t = $token;
		if (!t) return;
		activeView = 'chat';
		const pendingEffort = get(reasoningEffort);
		const inheritModel = $activeModelId ?? getLastChosenModelId();
		const body = buildNewThreadBody({
			projectId,
			reasoningEffort: pendingEffort,
			modelId: inheritModel,
		});
		const res = await fetch(`/api/threads?token=${t}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		});
		const data = await res.json();
		const thread: Thread = {
			id: data.thread_id,
			user_id: $user?.id ?? '',
			profile: 'personal',
			title: 'New Chat',
			model: inheritModel,
			project_id: projectId ?? undefined,
			reasoning_effort: pendingEffort ?? null,
			created_at: new Date().toISOString(),
			updated_at: new Date().toISOString(),
		};
		adoptCreatedThread(thread);
		activeThreadId.set(data.thread_id);
		messages.set([]);
		clearThreadBuffers(data.thread_id);
		sidebarOpen = false;
		
		subscribeThread(data.thread_id);
		await chatPanelRef?.fetchThreadMessages(data.thread_id);
	}

	const selectThread = createHomeThreadSelector({
		advanceIntent: () => selectionIntent++,
		prepareChatSurface: (firstRun) => { activeView = 'chat'; if (!firstRun) sidebarOpen = false; }, // a late first-run landing keeps the user's drawer
		loadThreads,
		selectChatThread: (id, source, owner) => chatPanelRef?.selectThread(id, source, owner),
	});

	function deleteThread(id: string) {
		deleteConfirmId = id;
	}

	async function removeThreadFromView(id: string) {
		threads.update(ss => ss.filter(s => s.id !== id));
		if ($activeThreadId === id) {
			const remaining = $threads;
			const next = remaining.length > 0 ? remaining[0].id : null;
			activeThreadId.set(next);
			if (next) {
				const nextThread = $threads.find(s => s.id === next);
				if (nextThread) await enterThreadScope(nextThread);
				await chatPanelRef?.fetchThreadMessages(next);
			} else {
				messages.set([]);
				navigateHome();
			}
		}
	}

	async function confirmDeleteThread() {
		const id = deleteConfirmId;
		deleteConfirmId = null;
		if (!id || !$token) return;
		const priorTurnState = getTurnState(id);
		resetTurnState(id);
		clearThreadBuffers(id);
		cancelThreadTypewriters(id);
		await removeThreadFromView(id);
		try {
			const response = await fetch(
				`/api/threads/${id}?token=${$token}`,
				{ method: 'DELETE' },
			);
			if (!response.ok) throw new Error(`DELETE thread ${id} -> ${response.status}`);
		} catch (error) {
			restoreTurnState(id, priorTurnState);
			console.error('Failed to delete thread', error);
		}
	}

	function cancelDeleteThread() { deleteConfirmId = null; }

	async function archiveThread(id: string) {
		if (!$token) return;
		await removeThreadFromView(id);
		await fetch(`/api/threads/${id}?token=${$token}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ archived: true }),
		}).catch(() => {});
	}

	async function loadThreads() {
		const t = $token;
		if (!t) return;
		const res = await fetch(`/api/threads?token=${t}`);
		const data = await res.json();
		const filtered = reconcileThreadModelSnapshots(data.threads as Thread[]);
		filtered.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
		threads.set(filtered);
		void fetchScopeTarget(t, $uiScope); // the list omits the target; re-read its model too
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			if (get(paneOpen)) { paneOpen.set(false); return; }
			if (sidebarOpen) { sidebarOpen = false; return; }
		}
		if ((e.metaKey || e.ctrlKey) && e.key === 'b') {
			e.preventDefault();
			$sidebarCollapsed = !$sidebarCollapsed;
		}
		if ((e.metaKey || e.ctrlKey) && (e.key === 'n' || e.key === 'N')) {
			e.preventDefault();
			focusEmptyChat();
		}
	}

	function focusEmptyChat() {
		resetSurface('chat');
		if (get(isMobile)) return;
		tick().then(() => {
			const ta = document.querySelector('.input-chip textarea') as HTMLTextAreaElement | null;
			ta?.focus();
		});
	}

	onMount(() => {
		return registerHomeLifecycle({
			token: () => $token,
			activeThreadId: () => get(activeThreadId),
			activeView: () => activeView,
			threads: () => get(threads),
			loadThreads,
			loadModels,
			loadSettings,
			loadUpdateStatus,
			pollSystem,
			checkSecurityAlert: () => checkSecurityAlert($token),
			selectThread,
			setActiveView: (view) => { activeView = view; },
			chatPanel: () => chatPanelRef,
			on,
			connect,
		});
	});
</script>

<svelte:head>
	<title>{$activeThread?.title || 'CORSIA'}</title>
</svelte:head>

<svelte:window onkeydown={handleKeydown} />


<HomePageView
	isAuthenticated={$isAuthenticated} {activeView} bind:sidebarOpen bind:chatPanelRef
	{conductorThreadId} {conductorKnownAbsent} {systemStatus} {isDraggingOver} {paneConfig}
	bind:lightboxUrl bind:lightboxOpen
	{deleteConfirmId}
	onNewChat={focusEmptyChat} onActivity={openActivity} onConductor={goConductor} onDocuments={goDocuments}
	onAttachProject={attachThreadToProject} onSelectThread={(id) => selectThread(id, 'sidebar')}
	onDeleteThread={deleteThread} onArchiveThread={archiveThread} onOpenSettings={openSettings}
	onToggleSidebar={toggleSidebar} onTogglePane={togglePane} onOpenLightbox={openLightbox}
	onAutoCreateThread={async () => { await newThread($uiScope?.id ?? undefined); }}
	onPickProject={pickProject} onPickProjectAndClosePane={pickProjectAndClosePane}
	onCloseSettings={() => closeSettingsBack((id) => selectThread(id, 'other'), openActivity)}
	onMeasurement={() => resetSurface('measurement', { preserveActiveThread: true })}
	onCloseMeasurement={() => closeMeasurementSurface({ resetSurface, setActiveThread: (threadId) => activeThreadId.set(threadId) })}
	onPickConversation={(id) => selectThread(id, 'sidebar')}
	onGoWorkspace={navigateHome} onProjectCreated={pickProject}
	onConfirmDeleteThread={confirmDeleteThread} onCancelDeleteThread={cancelDeleteThread}
/>
