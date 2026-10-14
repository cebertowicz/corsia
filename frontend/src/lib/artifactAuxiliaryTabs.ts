import type {
	ArtifactTab,
	OpenBrowserInput,
	OpenLegacyDiscussionInput,
	OpenSubagentInput,
	ThreadArtifactWorkspace,
} from './artifactTabTypes';

type InstanceIdFactory = () => string;

/** Adds a browser pane without changing the tab the user is viewing. */
export function openBrowserTab(
	ws: ThreadArtifactWorkspace,
	input: OpenBrowserInput,
	newInstanceId: InstanceIdFactory,
	activate = true,
): ThreadArtifactWorkspace {
	const existing = ws.tabs.findIndex((tab) => tab.kind === 'browser' && tab.threadId === input.threadId);
	if (existing >= 0) return activate ? { ...ws, activeTabId: ws.tabs[existing].instanceId, visible: true, activeSurface: 'artifact' } : ws;
	const tab: ArtifactTab = { kind: 'browser', instanceId: newInstanceId(), artifactId: null,
		path: `browser:${input.threadId}`, title: input.title ?? 'Browser', threadId: input.threadId,
		loadedRevision: null, latestRevision: null, status: 'ready' };
	return { ...ws, tabs: [...ws.tabs, tab], activeTabId: activate || !ws.activeTabId ? tab.instanceId : ws.activeTabId,
		visible: activate ? true : ws.visible, activeSurface: activate ? 'artifact' : ws.activeSurface };
}

/** Adds or selects a child conversation in the existing artifact leaf. */
export function openSubagentTab(
	ws: ThreadArtifactWorkspace,
	input: OpenSubagentInput,
	newInstanceId: InstanceIdFactory,
): ThreadArtifactWorkspace {
	const existing = ws.tabs.findIndex((tab) => tab.kind === 'subagent' && tab.threadId === input.threadId);
	if (existing >= 0) {
		const tabs = ws.tabs.slice();
		tabs[existing] = {
			...tabs[existing],
			title: input.title,
			// A re-open with no node (a chip, not a plan step) keeps whatever
			// return point an earlier plan-node open already established.
			...(input.sourceNodeId
				? { sourceNodeId: input.sourceNodeId, sourceStepId: input.sourceStepId }
				: {}),
		};
		return {
			...ws,
			tabs,
			activeTabId: tabs[existing].instanceId,
			visible: true,
			activeSurface: 'artifact',
		};
	}
	const tab: ArtifactTab = {
		kind: 'subagent',
		instanceId: newInstanceId(),
		artifactId: null,
		path: `thread:${input.threadId}`,
		title: input.title,
		threadId: input.threadId,
		sourceNodeId: input.sourceNodeId,
		sourceStepId: input.sourceStepId,
		loadedRevision: null,
		latestRevision: null,
		status: 'ready',
	};
	return {
		...ws,
		tabs: [...ws.tabs, tab],
		activeTabId: tab.instanceId,
		visible: true,
		activeSurface: 'artifact',
	};
}

/** Opens an existing plan discussion as a read-only artifact-leaf archive. */
export function openLegacyDiscussionTab(
	ws: ThreadArtifactWorkspace,
	input: OpenLegacyDiscussionInput,
	newInstanceId: InstanceIdFactory,
): ThreadArtifactWorkspace {
	const existing = ws.tabs.findIndex((tab) => tab.kind === 'legacy-discussion' && tab.artifactId === input.artifactId);
	if (existing >= 0) {
		const tabs = ws.tabs.slice();
		tabs[existing] = { ...tabs[existing], title: input.title };
		return {
			...ws,
			tabs,
			activeTabId: tabs[existing].instanceId,
			visible: true,
			activeSurface: 'artifact',
		};
	}
	const tab: ArtifactTab = {
		kind: 'legacy-discussion',
		instanceId: newInstanceId(),
		artifactId: input.artifactId,
		path: `discussion:${input.artifactId}`,
		title: input.title,
		threadId: null,
		loadedRevision: null,
		latestRevision: null,
		status: 'readonly',
	};
	return {
		...ws,
		tabs: [...ws.tabs, tab],
		activeTabId: tab.instanceId,
		visible: true,
		activeSurface: 'artifact',
	};
}
