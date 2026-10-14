// Pure tab model for the thread-scoped artifact workspace.
//
// Every transition that can be expressed without stores lives here so it is
// directly testable and so `stores/artifactWorkspace.ts` stays a thin, single
// owner of the reactive state. See C0-ARTIFACT-LEAF-SHELL-DESIGN.md.
//
// Two identities, deliberately separate:
//
// * `instanceId` — the *tab instance* key. Assigned once and never changed, so
//   the draft, save timer, close gate, and any in-flight load signal that
//   captured it can never be orphaned by something the tab later learns.
// * `artifactId` (primary) + `path` (fallback until resolved) — the *artifact*
//   identity. Both are enriched over time: `artifactId` fills in when the server
//   resolves it, and `path` follows the document if it relocates. Dedupe prefers
//   `artifactId`, so the same artifact never occupies two tabs in one thread
//   even after its path changes.

import {
	openBrowserTab as openAuxiliaryBrowserTab,
	openLegacyDiscussionTab as openAuxiliaryDiscussionTab,
	openSubagentTab as openAuxiliarySubagentTab,
} from './artifactAuxiliaryTabs';
import { restoreWorkspace as restorePersistedWorkspace } from './artifactWorkspaceRestore';
import type {
	ArtifactTab,
	DuplicateMergeGuard,
	ImageViewportPlace,
	OpenArtifactInput,
	OpenBrowserInput,
	OpenLegacyDiscussionInput,
	OpenSubagentInput,
	RevisionCheck,
	TabLoadSignal,
	ThreadArtifactWorkspace,
} from './artifactTabTypes';

export type {
	ArtifactTab,
	ArtifactTabStatus,
	DuplicateMergeGuard,
	ImageViewportPlace,
	OpenArtifactInput,
	OpenBrowserInput,
	OpenLegacyDiscussionInput,
	OpenSubagentInput,
	RevisionCheck,
	TabLoadSignal,
	ThreadArtifactWorkspace,
} from './artifactTabTypes';

let instanceCounter = 0;

function newInstanceId(): string {
	if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
		return crypto.randomUUID();
	}
	instanceCounter += 1;
	return `tab-${Date.now().toString(36)}-${instanceCounter}`;
}

export function emptyWorkspace(): ThreadArtifactWorkspace {
	return { tabs: [], activeTabId: null, visible: false, activeSurface: 'chat' };
}
export function tabKey(tab: Pick<ArtifactTab, 'instanceId'>): string {
	return tab.instanceId;
}

/** A newer content revision exists than the one this tab last loaded. */
export function isTabChanged(tab: ArtifactTab): boolean {
	if (tab.kind !== 'document') return false;
	if (tab.loadedRevision === null || tab.latestRevision === null) return false;
	return tab.loadedRevision !== tab.latestRevision;
}

export function getActiveTab(ws: ThreadArtifactWorkspace): ArtifactTab | null {
	return ws.tabs.find((t) => t.instanceId === ws.activeTabId) ?? null;
}

export function hasChangedTab(ws: ThreadArtifactWorkspace): boolean {
	return ws.tabs.some(isTabChanged);
}
/** Same document? Artifact identity decides once known; path is the fallback. */
function sameArtifact(tab: ArtifactTab, artifactId: string | null | undefined, path: string): boolean {
	if (tab.kind !== 'document') return false;
	if (artifactId && tab.artifactId) return tab.artifactId === artifactId;
	return tab.path === path;
}

/** Adds-or-selects a tab (dedupe by artifactId, else path) and reveals the leaf. */
export function openTab(ws: ThreadArtifactWorkspace, input: OpenArtifactInput): ThreadArtifactWorkspace {
	const existing = ws.tabs.findIndex((t) => sameArtifact(t, input.artifactId, input.path));
	if (existing >= 0) {
		const tabs = ws.tabs.slice();
		// Opening a known artifact enriches a tab that only had the path.
		if (input.artifactId && !tabs[existing].artifactId) {
			tabs[existing] = { ...tabs[existing], artifactId: input.artifactId };
		}
		return {
			...ws,
			tabs,
			activeTabId: tabs[existing].instanceId,
			visible: true,
			activeSurface: 'artifact',
		};
	}
	const tab: ArtifactTab = {
		kind: 'document',
		instanceId: newInstanceId(),
		artifactId: input.artifactId ?? null,
		path: input.path,
		title: input.title,
		threadId: null,
		loadedRevision: null,
		latestRevision: null,
		status: 'loading',
	};
	return {
		...ws,
		tabs: [...ws.tabs, tab],
		activeTabId: tab.instanceId,
		visible: true,
		activeSurface: 'artifact',
	};
}

/** Adds the browser tab for an explicit opener or background browser event. */
export function openBrowserTab(ws: ThreadArtifactWorkspace, input: OpenBrowserInput, activate = true): ThreadArtifactWorkspace {
	return openAuxiliaryBrowserTab(ws, input, newInstanceId, activate);
}

/** Adds or selects a child conversation in the existing artifact leaf. */
export function openSubagentTab(ws: ThreadArtifactWorkspace, input: OpenSubagentInput): ThreadArtifactWorkspace {
	return openAuxiliarySubagentTab(ws, input, newInstanceId);
}

/** Opens an existing plan discussion as a read-only artifact-leaf archive. */
export function openLegacyDiscussionTab(ws: ThreadArtifactWorkspace, input: OpenLegacyDiscussionInput): ThreadArtifactWorkspace {
	return openAuxiliaryDiscussionTab(ws, input, newInstanceId);
}

/** Removes a tab; closing the active tab selects a neighbor; the last close returns chat-only. */
export function closeTab(ws: ThreadArtifactWorkspace, instanceId: string): ThreadArtifactWorkspace | null {
	const idx = ws.tabs.findIndex((t) => t.instanceId === instanceId);
	if (idx < 0) return null;
	const tabs = ws.tabs.slice();
	tabs.splice(idx, 1);
	let activeTabId = ws.activeTabId;
	if (activeTabId === instanceId) {
		const next = tabs[Math.min(idx, tabs.length - 1)];
		activeTabId = next ? next.instanceId : null;
	}
	const hasTabs = tabs.length > 0;
	return {
		...ws,
		tabs,
		activeTabId,
		visible: hasTabs ? ws.visible : false,
		activeSurface: hasTabs ? ws.activeSurface : 'chat',
	};
}

/**
 * Writes one changed tab back, folding it into an existing tab for the same
 * artifact when the resolved id proves they are the same document.
 *
 * The surviving tab keeps the *earlier* instance key so its draft and pending
 * save stay addressable; only the later duplicate is dropped.
 */
function commitTab(ws: ThreadArtifactWorkspace, idx: number, next: ArtifactTab, canMergeDuplicate: DuplicateMergeGuard = () => true): ThreadArtifactWorkspace {
	const tabs = ws.tabs.slice();
	tabs[idx] = next;
	if (!next.artifactId) return { ...ws, tabs };
	const twin = tabs.findIndex((t, i) => i !== idx && t.artifactId === next.artifactId);
	if (twin < 0) return { ...ws, tabs };
	const keepIdx = Math.min(twin, idx);
	const dropIdx = Math.max(twin, idx);
	if (!canMergeDuplicate(tabs[keepIdx].instanceId, tabs[dropIdx].instanceId)) {
		// Keep this instance's unresolved identity until its draft settles. A
		// reducer cannot discard a draft/save owner it does not also transfer.
		tabs[idx] = { ...next, artifactId: ws.tabs[idx].artifactId };
		return { ...ws, tabs };
	}
	const survivor: ArtifactTab = {
		...tabs[keepIdx],
		artifactId: next.artifactId,
		path: next.path,
		status: next.status,
		loadedRevision: keepIdx === idx ? next.loadedRevision : tabs[keepIdx].loadedRevision,
		latestRevision: next.latestRevision ?? tabs[keepIdx].latestRevision,
	};
	const droppedId = tabs[dropIdx].instanceId;
	const merged = tabs.filter((_, i) => i !== dropIdx);
	merged[keepIdx] = survivor;
	return {
		...ws,
		tabs: merged,
		activeTabId: ws.activeTabId === droppedId ? survivor.instanceId : ws.activeTabId,
	};
}

function unchanged(a: ArtifactTab, b: ArtifactTab): boolean {
	return a.status === b.status && a.artifactId === b.artifactId && a.path === b.path && a.loadedRevision === b.loadedRevision && a.latestRevision === b.latestRevision;
}

/**
 * Applies one load result to the tab that initiated it.
 *
 * A successful load makes the loaded revision current, which is exactly what
 * clears the changed dot; it never touches any other tab, so the same artifact
 * open in another thread keeps its own (stale) baseline.
 */
export function applyLoadSignal(ws: ThreadArtifactWorkspace, signal: TabLoadSignal, canMergeDuplicate?: DuplicateMergeGuard): ThreadArtifactWorkspace | null {
	const idx = ws.tabs.findIndex((t) => t.instanceId === signal.instanceId);
	if (idx < 0) return null;
	const prev = ws.tabs[idx];
	if (prev.kind !== 'document') return null;
	const loaded = signal.status === 'ready' && !!signal.revision;
	const next: ArtifactTab = {
		...prev,
		status: signal.status === 'ready' && signal.writable === false ? 'readonly' : signal.status,
		loadedRevision: loaded ? signal.revision! : prev.loadedRevision,
		latestRevision: loaded ? signal.revision! : prev.latestRevision,
		artifactId: signal.artifactId ?? prev.artifactId,
	};
	if (unchanged(prev, next)) return null;
	return commitTab(ws, idx, next, canMergeDuplicate);
}

/**
 * Records what a read-only revision probe observed for a tab.
 *
 * This is what dots an unfocused tab. It never advances the loaded revision, so
 * a tab only stops showing the dot once it actually reloads the newer content.
 * A resolved artifact that has moved relocates the tab rather than reporting it
 * as gone.
 */
export function applyRevisionCheck(ws: ThreadArtifactWorkspace, instanceId: string, check: RevisionCheck, canMergeDuplicate?: DuplicateMergeGuard): ThreadArtifactWorkspace | null {
	const idx = ws.tabs.findIndex((t) => t.instanceId === instanceId);
	if (idx < 0) return null;
	const prev = ws.tabs[idx];
	if (prev.kind !== 'document') return null;
	if (!check.exists) {
		if (prev.status === 'unavailable') return null;
		return commitTab(ws, idx, { ...prev, status: 'unavailable' });
	}
	const next: ArtifactTab = {
		...prev,
		status: prev.status === 'unavailable' ? 'loading' : prev.status === 'ready' && !check.writable ? 'readonly' : prev.status === 'readonly' && check.writable ? 'ready' : prev.status,
		artifactId: prev.artifactId ?? check.artifactId,
		path: check.path ?? prev.path,
		latestRevision: check.revision ?? prev.latestRevision,
	};
	if (unchanged(prev, next)) return null;
	return commitTab(ws, idx, next, canMergeDuplicate);
}

/**
 * Attaches a newly resolved artifact id to a tab that had none.
 *
 * Used when something other than the load resolves the identity — registering
 * an artifact in order to post the first comment — so the tab, its comments,
 * and its change baseline keep referring to the same document.
 */
export function resolveTabArtifact(ws: ThreadArtifactWorkspace, instanceId: string, artifactId: string, canMergeDuplicate?: DuplicateMergeGuard): ThreadArtifactWorkspace | null {
	const idx = ws.tabs.findIndex((t) => t.instanceId === instanceId);
	if (idx < 0 || ws.tabs[idx].kind !== 'document' || ws.tabs[idx].artifactId === artifactId) return null;
	return commitTab(ws, idx, { ...ws.tabs[idx], artifactId }, canMergeDuplicate);
}

/**
 * Records a save this tab performed.
 *
 * The saved revision becomes this tab's own baseline, which is exactly what
 * scopes own-save suppression to the originating tab instance: the same
 * artifact open in another thread keeps its older baseline and therefore dots.
 */
export function applyOwnSave(ws: ThreadArtifactWorkspace, instanceId: string, revision: string): ThreadArtifactWorkspace | null {
	const idx = ws.tabs.findIndex((t) => t.instanceId === instanceId);
	if (idx < 0) return null;
	const prev = ws.tabs[idx];
	if (prev.kind !== 'document') return null;
	if (prev.loadedRevision === revision && prev.latestRevision === revision) return null;
	const tabs = ws.tabs.slice();
	tabs[idx] = { ...prev, loadedRevision: revision, latestRevision: revision };
	return { ...ws, tabs };
}

/** Renames a tab once the server reports the document's real title. */
export function renameTab(ws: ThreadArtifactWorkspace, instanceId: string, title: string): ThreadArtifactWorkspace | null {
	const idx = ws.tabs.findIndex((t) => t.instanceId === instanceId);
	if (idx < 0 || !title || ws.tabs[idx].title === title) return null;
	const tabs = ws.tabs.slice();
	tabs[idx] = { ...tabs[idx], title };
	return { ...ws, tabs };
}

/** Restores a persisted workspace without trusting server-derived document state. */
export function restoreWorkspace(raw: unknown): ThreadArtifactWorkspace | null {
	return restorePersistedWorkspace(raw, newInstanceId);
}
