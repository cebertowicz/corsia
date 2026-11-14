// A pressed link's tab, open in its loading state while the link resolves to a file.
//
// The press shows the tab at once; which file it names is only known after the
// scope roots and the file's revision come back. The tab carries the link until
// then and is never persisted: a reload starts nothing it could wait on.

import { openTab } from './artifactTabs';
import type { ArtifactLink, OpenArtifactInput, ThreadArtifactWorkspace } from './artifactTabTypes';

function sameLink(a: ArtifactLink, b: ArtifactLink): boolean {
	return a.href === b.href && a.projectId === b.projectId;
}

/**
 * Opens a loading tab for a link press, or selects the tab the same link already has.
 * `started` is false when that link is still resolving, so the caller starts nothing new;
 * a tab the link failed in loads again.
 */
export function openLinkTab(
	ws: ThreadArtifactWorkspace, link: ArtifactLink, title: string, newInstanceId: () => string,
): { ws: ThreadArtifactWorkspace; instanceId: string; started: boolean } {
	const existing = ws.tabs.find((tab) => tab.kind === 'document' && tab.link && sameLink(tab.link, link));
	if (existing) {
		const started = existing.status !== 'loading';
		const tabs = started ? ws.tabs.map((tab) => (tab === existing ? { ...tab, status: 'loading' as const } : tab)) : ws.tabs;
		return { ws: { ...ws, tabs, activeTabId: existing.instanceId, visible: true, activeSurface: 'artifact' }, instanceId: existing.instanceId, started };
	}
	const instanceId = newInstanceId();
	const tab = {
		kind: 'document' as const, instanceId, artifactId: null, path: '', title, threadId: null,
		loadedRevision: null, latestRevision: null, status: 'loading' as const, link,
	};
	return { ws: { ...ws, tabs: [...ws.tabs, tab], activeTabId: instanceId, visible: true, activeSurface: 'artifact' }, instanceId, started: true };
}

/**
 * The link resolved: the loading tab becomes that document's tab, or gives way to the tab
 * the document already has. Nothing opens if the person closed the loading tab meanwhile.
 */
export function settleLinkTab(ws: ThreadArtifactWorkspace, instanceId: string, input: OpenArtifactInput): ThreadArtifactWorkspace | null {
	const index = ws.tabs.findIndex((tab) => tab.instanceId === instanceId);
	if (index < 0) return null;
	const wasActive = ws.activeTabId === instanceId;
	const rest = { ...ws, tabs: ws.tabs.filter((tab) => tab.instanceId !== instanceId) };
	const opened = openTab(rest, input);
	const added = opened.tabs.length > rest.tabs.length;
	const settledId = added ? opened.tabs[opened.tabs.length - 1].instanceId : opened.activeTabId;
	const tabs = opened.tabs.slice();
	if (added) {
		// Keep the tab's place and instance: it is the same tab, now knowing its file.
		const settled = { ...tabs.pop()!, instanceId };
		tabs.splice(index, 0, settled);
	}
	const activeTabId = wasActive ? (added ? instanceId : settledId) : ws.activeTabId;
	return { ...opened, tabs, activeTabId, visible: ws.visible || wasActive, activeSurface: wasActive ? 'artifact' : ws.activeSurface };
}
