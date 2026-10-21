import { writable, get } from 'svelte/store';
import {
	applyLoadSignal, applyOwnSave, applyRevisionCheck, closeTab, emptyWorkspace, newInstanceId, openTab,
	resolveTabArtifact, restoreWorkspace, tabKey, type ArtifactTabStatus,
	type OpenArtifactInput, openLegacyDiscussionTab, type OpenLegacyDiscussionInput,
	openSubagentTab, type OpenSubagentInput, type RevisionCheck, type TabLoadSignal,
	type ThreadArtifactWorkspace,
} from '$lib/artifactTabs';
import { openBrowserTab } from '$lib/artifactAuxiliaryTabs';
import { clearDraft, existingDraft, relocateDraftTarget } from '$lib/stores/docSaveQueue';
import { closeLiveDocument } from '$lib/stores/liveDocuments';
import { token } from '$lib/stores/auth';
import { clearTabViewState } from '$lib/stores/tabViewState';

export {
	emptyWorkspace, tabKey, getActiveTab, isTabChanged, hasChangedTab,
} from '$lib/artifactTabs';
export type {
	ArtifactTab, ArtifactTabStatus, OpenArtifactInput, OpenLegacyDiscussionInput, OpenSubagentInput, RevisionCheck, TabLoadSignal,
	ThreadArtifactWorkspace,
} from '$lib/artifactTabs';

const STORAGE_KEY = 'corsia:artifactWorkspace:v1';

interface PersistedShape {
	threads: Record<string, ThreadArtifactWorkspace>;
	splitWidth: number | null;
}

function loadPersisted(): PersistedShape {
	if (typeof localStorage === 'undefined') return { threads: {}, splitWidth: null };
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return { threads: {}, splitWidth: null };
		const parsed = JSON.parse(raw);
		if (!parsed || typeof parsed !== 'object') return { threads: {}, splitWidth: null };
		const threads: Record<string, ThreadArtifactWorkspace> = {};
		for (const [threadId, value] of Object.entries(parsed.threads ?? {})) {
			const restored = restoreWorkspace(value);
			if (restored && restored.tabs.length) threads[threadId] = restored;
		}
		const splitWidthValue = typeof parsed.splitWidth === 'number' ? parsed.splitWidth : null;
		return { threads, splitWidth: splitWidthValue };
	} catch {
		return { threads: {}, splitWidth: null };
	}
}

const persisted = loadPersisted();

export const threadWorkspaces = writable<Record<string, ThreadArtifactWorkspace>>(persisted.threads);
export const splitWidth = writable<number | null>(persisted.splitWidth);

function persistentWorkspace(
	workspace: ThreadArtifactWorkspace,
): ThreadArtifactWorkspace | null {
	const tabs = workspace.tabs.filter((tab) => tab.kind === 'document');
	if (tabs.length === 0) return null;
	const activeTabId = tabs.some((tab) => tab.instanceId === workspace.activeTabId)
		? workspace.activeTabId
		: tabs[0].instanceId;
	return { ...workspace, tabs, activeTabId };
}

function persistentThreads(): Record<string, ThreadArtifactWorkspace> {
	const kept: Record<string, ThreadArtifactWorkspace> = {};
	for (const [threadId, workspace] of Object.entries(get(threadWorkspaces))) {
		const persistent = persistentWorkspace(workspace);
		if (persistent) kept[threadId] = persistent;
	}
	return kept;
}

function persist(): void {
	if (typeof localStorage === 'undefined') return;
	try {
		const payload: PersistedShape = { threads: persistentThreads(), splitWidth: get(splitWidth) };
		localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
	} catch {
		// Best-effort — quota or private-mode failures leave in-memory state intact.
	}
}

function stripAuxiliaryTabs(): void {
	threadWorkspaces.update((all) => {
		let changed = false;
		const next: Record<string, ThreadArtifactWorkspace> = {};
		for (const [threadId, workspace] of Object.entries(all)) {
			const tabs = workspace.tabs.filter((tab) => tab.kind === 'document');
			if (tabs.length === workspace.tabs.length) {
				next[threadId] = workspace;
				continue;
			}
			changed = true;
			next[threadId] = tabs.length
				? {
					...workspace,
					tabs,
					activeTabId: tabs.some((tab) => tab.instanceId === workspace.activeTabId)
						? workspace.activeTabId
						: tabs[0].instanceId,
				}
				: emptyWorkspace();
		}
		return changed ? next : all;
	});
}

threadWorkspaces.subscribe(persist);
splitWidth.subscribe(persist);

// Threads whose browser an agent has revealed since this account signed in to this app session.
const revealedBrowsers = new Set<string>();

let lastToken = get(token);
token.subscribe((value) => {
	if (value === lastToken) return;
	lastToken = value;
	revealedBrowsers.clear();
	stripAuxiliaryTabs();
});

/** Applies one pure reducer to a thread's workspace; `null` means "no change". */
function draftBlocksMerge(instanceId: string): boolean {
	const draft = existingDraft({ instanceId });
	return !!draft && (
		draft.editText !== draft.savedText || draft.status === 'saving' || draft.status === 'error'
	);
}

function canMergeDuplicateTabs(survivorInstanceId: string, duplicateInstanceId: string): boolean {
	return !draftBlocksMerge(survivorInstanceId) && !draftBlocksMerge(duplicateInstanceId);
}

function mutate(
	threadId: string,
	update: (ws: ThreadArtifactWorkspace) => ThreadArtifactWorkspace | null,
): string[] {
	const removed: string[] = [];
	threadWorkspaces.update((all) => {
		const current = all[threadId] ?? emptyWorkspace();
		const next = update(current);
		if (!next || next === current) return all;
		const remaining = new Set(next.tabs.map((tab) => tab.instanceId));
		for (const tab of current.tabs) if (!remaining.has(tab.instanceId)) removed.push(tab.instanceId);
		return { ...all, [threadId]: next };
	});
	// Every tab write passes here, so a draft's path follows its tab's whichever reducer moved it.
	for (const tab of get(threadWorkspaces)[threadId]?.tabs ?? []) {
		if (tab.kind === 'document') relocateDraftTarget(tab, tab.path);
	}
	// Every dropped tab takes its live binding and in-session camera/place with it, however it was removed.
	for (const instanceId of removed) {
		closeLiveDocument(instanceId);
		clearTabViewState(threadId, instanceId);
	}
	return removed;
}

export function openArtifactTab(threadId: string, input: OpenArtifactInput): void {
	mutate(threadId, (ws) => openTab(ws, input));
}

/** Applies an agent request without overriding the user's chosen canvas surface. */
export function openAgentDocumentTab(threadId: string, input: OpenArtifactInput): void {
	mutate(threadId, (ws) => ({ ...openTab(ws, input), activeSurface: ws.activeSurface }));
}

/**
 * Opens a browser pane. Only an agent's first reveal of a thread's browser in this app session takes
 * the view; a later one adds a missing tab in the background, even after the user closed it.
 */
export function openBrowserPane(threadId: string, agentOpened = false): void {
	const foreground = !agentOpened || !revealedBrowsers.has(threadId);
	if (agentOpened) revealedBrowsers.add(threadId);
	mutate(threadId, (ws) => openBrowserTab(ws, { threadId }, newInstanceId, foreground));
}

/** Update this thread's sole browser tab without changing its selection or lifecycle. */
export function setBrowserPaneLabel(threadId: string, title: string): void {
	mutate(threadId, (ws) => {
		let changed = false;
		const tabs = ws.tabs.map((tab) => {
			if (tab.kind !== 'browser' || tab.title === title) return tab;
			changed = true;
			return { ...tab, title };
		});
		return changed ? { ...ws, tabs } : null;
	});
}

/** Opens a canonical child conversation beside its still-mounted parent. */
export function openSubagentConversationTab(threadId: string, input: OpenSubagentInput): void {
	mutate(threadId, (ws) => openSubagentTab(ws, input));
}

/** Preserves read-only access to a pre-existing plan discussion artifact. */
export function openLegacyDiscussionArchiveTab(threadId: string, input: OpenLegacyDiscussionInput): void {
	mutate(threadId, (ws) => openLegacyDiscussionTab(ws, input));
}

export function closeArtifactTab(threadId: string, instanceId: string): void {
	mutate(threadId, (ws) => closeTab(ws, instanceId));
}

export function setActiveTab(threadId: string, instanceId: string): void {
	mutate(threadId, (ws) => {
		if (ws.activeTabId === instanceId || !ws.tabs.some((t) => t.instanceId === instanceId)) return null;
		return { ...ws, activeTabId: instanceId, activeSurface: 'artifact' };
	});
}

/** Set only by click/typing inside a pane — never hover, scroll, or programmatic focus. */
export function setActiveSurface(threadId: string, surface: 'chat' | 'artifact'): void {
	mutate(threadId, (ws) => {
		if (ws.activeSurface === surface) return null;
		if (surface === 'artifact' && ws.tabs.length === 0) return null;
		return { ...ws, activeSurface: surface };
	});
}

export function setLeafVisible(threadId: string, visible: boolean): void {
	mutate(threadId, (ws) => {
		if (ws.visible === visible) return null;
		return { ...ws, visible };
	});
}

/** Applies an async load result to the tab that initiated it, never the active one. */
function mutateWithDraftAwareMerge(
	threadId: string,
	update: (ws: ThreadArtifactWorkspace) => ThreadArtifactWorkspace | null,
): void {
	// A successful safe merge retires any settled dropped draft with its tab.
	for (const instanceId of mutate(threadId, update)) clearDraft({ instanceId });
}

export function applyTabLoadSignal(threadId: string, signal: TabLoadSignal): void {
	mutateWithDraftAwareMerge(threadId, (ws) => applyLoadSignal(ws, signal, canMergeDuplicateTabs));
}

export function applyTabRevisionCheck(threadId: string, instanceId: string, check: RevisionCheck): void {
	mutateWithDraftAwareMerge(threadId, (ws) => applyRevisionCheck(ws, instanceId, check, canMergeDuplicateTabs));
}

/** Propagates an artifact id resolved outside the load path (e.g. by commenting). */
export function resolveArtifactTab(threadId: string, instanceId: string, artifactId: string): void {
	mutateWithDraftAwareMerge(threadId, (ws) => resolveTabArtifact(ws, instanceId, artifactId, canMergeDuplicateTabs));
}

/** Rebaselines the tab that performed a save, so only it stops showing a dot. */
export function markTabSaved(threadId: string, instanceId: string, revision: string | null): void {
	if (!revision) return;
	mutate(threadId, (ws) => applyOwnSave(ws, instanceId, revision));
}

export function markTabStatus(threadId: string, instanceId: string, status: ArtifactTabStatus): void {
	mutate(threadId, (ws) => {
		const idx = ws.tabs.findIndex((t) => t.instanceId === instanceId);
		if (idx < 0 || ws.tabs[idx].status === status) return null;
		const tabs = ws.tabs.slice();
		tabs[idx] = { ...tabs[idx], status };
		return { ...ws, tabs };
	});
}
