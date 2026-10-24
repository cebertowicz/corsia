import type { ArtifactTab, ThreadArtifactWorkspace } from './artifactTabTypes';

type InstanceIdFactory = () => string;

function sameDocument(tab: ArtifactTab, artifactId: string | null, path: string): boolean {
	if (tab.kind !== 'document') return false;
	if (artifactId && tab.artifactId) return tab.artifactId === artifactId;
	return tab.path === path;
}

/**
 * Restores a persisted workspace.
 *
 * Documents return to loading because nothing carried across a reload has been
 * verified against the server. Read-only discussions and child conversations
 * retain only their bounded identities and presentation state.
 */
export function restoreWorkspace(
	raw: unknown,
	newInstanceId: InstanceIdFactory,
): ThreadArtifactWorkspace | null {
	if (!raw || typeof raw !== 'object') return null;
	const value = raw as Partial<ThreadArtifactWorkspace>;
	if (!Array.isArray(value.tabs)) return null;
	const tabs: ArtifactTab[] = [];
	for (const entry of value.tabs) {
		if (!entry || typeof entry !== 'object') continue;
		// Auxiliary conversations and legacy discussion identities are scoped to
		// the authenticated browser session and never restored across accounts.
		if (entry.kind === 'legacy-discussion' || entry.kind === 'subagent') continue;
		if (entry.kind === 'browser' && typeof entry.threadId === 'string') {
			tabs.push({
				kind: 'browser',
				instanceId: typeof entry.instanceId === 'string' ? entry.instanceId : newInstanceId(),
				artifactId: null, path: `browser:${entry.threadId}`, title: 'Browser',
				threadId: entry.threadId, loadedRevision: null, latestRevision: null, status: 'ready',
			});
			continue;
		}
		if (typeof entry.path !== 'string') continue;
		const artifactId = typeof entry.artifactId === 'string' ? entry.artifactId : null;
		if (tabs.some((tab) => sameDocument(tab, artifactId, entry.path))) continue;
		tabs.push({
			kind: 'document',
			// Pre-instance-key persisted tabs keyed on artifact identity; adopt it
			// as this instance's key so a restored draft still resolves.
			instanceId: typeof entry.instanceId === 'string' && entry.instanceId ? entry.instanceId : (artifactId ?? `path:${entry.path}`),
			artifactId,
			path: entry.path,
			title: typeof entry.title === 'string' ? entry.title : entry.path,
			threadId: null,
			loadedRevision: typeof entry.loadedRevision === 'string' ? entry.loadedRevision : null,
			latestRevision: null,
			status: 'loading',
			...(entry.landOn === 'new-replies' ? { landOn: 'new-replies' as const } : {}),
		});
	}
	if (!tabs.length) return null;
	const activeTabId = tabs.some((tab) => tab.instanceId === value.activeTabId)
		? (value.activeTabId as string)
		: tabs[0].instanceId;
	return {
		tabs,
		activeTabId,
		visible: value.visible !== false,
		activeSurface: value.activeSurface === 'artifact' ? 'artifact' : 'chat',
	};
}
