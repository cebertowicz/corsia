import type {
	ArtifactTab,
	OpenLegacyDiscussionInput,
	OpenSubagentInput,
	ThreadArtifactWorkspace,
} from './artifactTabTypes';

type InstanceIdFactory = () => string;

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
