import { describe, expect, it } from 'vitest';
import { applyLoadSignal, applyOwnSave, applyRevisionCheck, closeTab, emptyWorkspace, getActiveTab, hasChangedTab, isTabChanged, openLegacyDiscussionTab, openSubagentTab, openTab, renameTab, resolveTabArtifact, restoreWorkspace, type RevisionCheck, type ThreadArtifactWorkspace } from './artifactTabs';

function withTabs(...opens: { artifactId?: string | null; path: string; title: string }[]): ThreadArtifactWorkspace {
	return opens.reduce((ws, input) => openTab(ws, input), emptyWorkspace());
}

function idOf(ws: ThreadArtifactWorkspace, path: string): string {
	const tab = ws.tabs.find((t) => t.path === path);
	if (!tab) throw new Error(`no tab at ${path}`);
	return tab.instanceId;
}

const found = (over: Partial<RevisionCheck> = {}): RevisionCheck => ({
	exists: true,
	path: null,
	revision: 'r2',
	writable: true,
	artifactId: null,
	...over,
});

describe('legacy discussion archive leaf tabs', () => {
	it('opens a read-only existing discussion by artifact identity without affecting documents', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		ws = openLegacyDiscussionTab(ws, {
			artifactId: 'discussion-1',
			title: 'Discussion archive — Task',
		});

		expect(ws.tabs).toHaveLength(2);
		expect(getActiveTab(ws)).toMatchObject({
			kind: 'legacy-discussion',
			artifactId: 'discussion-1',
			title: 'Discussion archive — Task',
			status: 'readonly',
		});
		expect(ws.activeSurface).toBe('artifact');

		const firstId = ws.activeTabId;
		ws = openLegacyDiscussionTab(ws, {
			artifactId: 'discussion-1',
			title: 'Updated title',
		});
		expect(ws.tabs).toHaveLength(2);
		expect(ws.activeTabId).toBe(firstId);
		expect(getActiveTab(ws)?.title).toBe('Updated title');
	});

	it('ignores document load signals for a legacy discussion archive', () => {
		const ws = openLegacyDiscussionTab(emptyWorkspace(), {
			artifactId: 'discussion-1',
			title: 'Discussion archive',
		});
		expect(
			applyLoadSignal(ws, {
				instanceId: ws.activeTabId!,
				status: 'unavailable',
			}),
		).toBeNull();
	});
});

describe('subagent conversation leaf tabs', () => {
	it('opens beside the parent, dedupes by child thread, and preserves document tabs', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		ws = openSubagentTab(ws, {
			threadId: 'child-1',
			title: 'Delegated work',
			sourceNodeId: 'node-1',
		});
		const firstId = ws.activeTabId;

		expect(ws.tabs).toHaveLength(2);
		expect(getActiveTab(ws)).toMatchObject({
			kind: 'subagent',
			threadId: 'child-1',
			title: 'Delegated work',
			status: 'ready',
		});
		expect(ws.activeSurface).toBe('artifact');
		expect(ws.visible).toBe(true);

		ws = openSubagentTab(ws, {
			threadId: 'child-1',
			title: 'Renamed delegated work',
			sourceNodeId: 'node-2',
		});
		expect(ws.tabs).toHaveLength(2);
		expect(ws.activeTabId).toBe(firstId);
		expect(getActiveTab(ws)?.title).toBe('Renamed delegated work');
	});

	it('keeps a nested return point with its owning step: a chip re-open keeps both, a top-level open replaces both', () => {
		let ws = openSubagentTab(emptyWorkspace(), {
			threadId: 'child-1', title: 'Nested', sourceNodeId: 'sub', sourceStepId: 'spawn',
		});
		expect(getActiveTab(ws)).toMatchObject({ sourceNodeId: 'sub', sourceStepId: 'spawn' });

		ws = openSubagentTab(ws, { threadId: 'child-1', title: 'Nested' });
		expect(getActiveTab(ws)).toMatchObject({ sourceNodeId: 'sub', sourceStepId: 'spawn' });

		ws = openSubagentTab(ws, { threadId: 'child-1', title: 'Top', sourceNodeId: 'top' });
		expect(getActiveTab(ws)?.sourceNodeId).toBe('top');
		expect(getActiveTab(ws)?.sourceStepId).toBeUndefined();
	});

	it('does not apply document load signals to a subagent conversation tab', () => {
		let ws = openSubagentTab(emptyWorkspace(), {
			threadId: 'child-1',
			title: 'Delegated work',
			sourceNodeId: 'node-1',
		});
		expect(
			applyLoadSignal(ws, {
				instanceId: ws.activeTabId!,
				status: 'unavailable',
			}),
		).toBeNull();
		expect(getActiveTab(ws)?.status).toBe('ready');
	});
});

describe('load signals apply to the tab that initiated them', () => {
	it('stamps the initiating tab even when another tab became active first', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' }, { path: '/b.md', title: 'b' });
		const slowTab = idOf(ws, '/a.md');
		expect(ws.activeTabId).toBe(idOf(ws, '/b.md')); // user moved on

		ws = applyLoadSignal(ws, {
			instanceId: slowTab,
			status: 'ready',
			revision: 'r1',
		})!;
		expect(ws.tabs.find((t) => t.instanceId === slowTab)!.status).toBe('ready');
		expect(getActiveTab(ws)!.status).toBe('loading'); // untouched
	});

	it('drops a signal whose tab has already been closed', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		const gone = idOf(ws, '/a.md');
		ws = closeTab(ws, gone)!;
		expect(
			applyLoadSignal(ws, {
				instanceId: gone,
				status: 'ready',
				revision: 'r1',
			}),
		).toBeNull();
		expect(ws.tabs).toHaveLength(0);
	});

	it('reports a load with no write permission as readonly, not ready', () => {
		let ws = withTabs({ path: '/context/SYSTEM.md', title: 'SYSTEM.md' });
		ws = applyLoadSignal(ws, {
			instanceId: idOf(ws, '/context/SYSTEM.md'),
			status: 'ready',
			revision: 'r1',
			writable: false,
		})!;
		expect(ws.tabs[0].status).toBe('readonly');
	});

	it('enriches artifact identity without changing the instance key', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		const instanceId = idOf(ws, '/a.md');
		ws = applyLoadSignal(ws, {
			instanceId,
			status: 'ready',
			revision: 'r1',
			artifactId: 'art-1',
		})!;
		expect(ws.tabs[0].artifactId).toBe('art-1');
		expect(ws.tabs[0].instanceId).toBe(instanceId);
		expect(ws.activeTabId).toBe(instanceId);
	});
});

describe('changed-dot semantics', () => {
	it('is not changed before any revision is known', () => {
		const ws = withTabs({ path: '/a.md', title: 'a' });
		expect(isTabChanged(ws.tabs[0])).toBe(false);
	});

	it('dots a tab when a check finds a newer revision than it loaded', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		const instanceId = idOf(ws, '/a.md');
		ws = applyLoadSignal(ws, { instanceId, status: 'ready', revision: 'r1' })!;
		expect(isTabChanged(ws.tabs[0])).toBe(false);

		ws = applyRevisionCheck(ws, instanceId, found({ revision: 'r2' }))!;
		expect(isTabChanged(ws.tabs[0])).toBe(true);
		expect(hasChangedTab(ws)).toBe(true);
	});

	it('clears only when the newer revision actually loads', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		const instanceId = idOf(ws, '/a.md');
		ws = applyLoadSignal(ws, { instanceId, status: 'ready', revision: 'r1' })!;
		ws = applyRevisionCheck(ws, instanceId, found({ revision: 'r2' }))!;
		expect(isTabChanged(ws.tabs[0])).toBe(true);

		ws = applyLoadSignal(ws, { instanceId, status: 'ready', revision: 'r2' })!;
		expect(isTabChanged(ws.tabs[0])).toBe(false);
	});

	it('scopes own-save suppression to the saving tab instance', () => {
		// Same document, two threads — modelled as two independent workspaces.
		let saver = withTabs({ artifactId: 'art-1', path: '/a.md', title: 'a' });
		let other = withTabs({ artifactId: 'art-1', path: '/a.md', title: 'a' });
		const saverId = idOf(saver, '/a.md');
		const otherId = idOf(other, '/a.md');
		saver = applyLoadSignal(saver, {
			instanceId: saverId,
			status: 'ready',
			revision: 'r1',
		})!;
		other = applyLoadSignal(other, {
			instanceId: otherId,
			status: 'ready',
			revision: 'r1',
		})!;

		saver = applyOwnSave(saver, saverId, 'r2')!;
		other = applyRevisionCheck(other, otherId, found({ revision: 'r2' }))!;

		expect(isTabChanged(saver.tabs[0])).toBe(false);
		expect(isTabChanged(other.tabs[0])).toBe(true);
	});
});

describe('availability and relocation', () => {
	it('marks a deleted document unavailable', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		ws = applyRevisionCheck(ws, idOf(ws, '/a.md'), found({ exists: false, revision: null }))!;
		expect(ws.tabs[0].status).toBe('unavailable');
	});

	it('relocates a resolved artifact instead of reporting it gone', () => {
		let ws = withTabs({ artifactId: 'art-1', path: '/docs/a.md', title: 'a' });
		const instanceId = idOf(ws, '/docs/a.md');
		ws = applyRevisionCheck(ws, instanceId, found({ path: '/archive/a.md', revision: 'r9' }))!;
		expect(ws.tabs[0].path).toBe('/archive/a.md');
		expect(ws.tabs[0].instanceId).toBe(instanceId);
		expect(ws.tabs[0].status).not.toBe('unavailable');
	});

	it('recovers an unavailable tab when its document comes back', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		const instanceId = idOf(ws, '/a.md');
		ws = applyRevisionCheck(ws, instanceId, found({ exists: false, revision: null }))!;
		ws = applyRevisionCheck(ws, instanceId, found({ revision: 'r3' }))!;
		expect(ws.tabs[0].status).toBe('loading');
	});

	it('flips between ready and readonly as write permission changes', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		const instanceId = idOf(ws, '/a.md');
		ws = applyLoadSignal(ws, { instanceId, status: 'ready', revision: 'r1' })!;
		ws = applyRevisionCheck(ws, instanceId, found({ writable: false }))!;
		expect(ws.tabs[0].status).toBe('readonly');
		ws = applyRevisionCheck(ws, instanceId, found({ writable: true, revision: 'r3' }))!;
		expect(ws.tabs[0].status).toBe('ready');
	});
});

describe('merging duplicates that resolve to one artifact', () => {
	it('folds a path-only tab into the artifact tab and keeps the earlier instance', () => {
		let ws = withTabs({ artifactId: 'art-1', path: '/docs/a.md', title: 'a' }, { path: '/link/a.md', title: 'a-alias' });
		const survivor = idOf(ws, '/docs/a.md');
		const duplicate = idOf(ws, '/link/a.md');

		ws = applyLoadSignal(ws, {
			instanceId: duplicate,
			status: 'ready',
			revision: 'r4',
			artifactId: 'art-1',
		})!;
		expect(ws.tabs).toHaveLength(1);
		expect(ws.tabs[0].instanceId).toBe(survivor);
		expect(ws.activeTabId).toBe(survivor);
		expect(ws.tabs[0].artifactId).toBe('art-1');
	});
});

describe('identity resolved outside the load path', () => {
	it('propagates an id registered in order to comment', () => {
		let ws = withTabs({ path: '/a.md', title: 'a' });
		const instanceId = idOf(ws, '/a.md');
		ws = resolveTabArtifact(ws, instanceId, 'art-9')!;
		expect(ws.tabs[0].artifactId).toBe('art-9');
		expect(ws.tabs[0].instanceId).toBe(instanceId);
		expect(resolveTabArtifact(ws, instanceId, 'art-9')).toBeNull();
	});

	it('renames a tab from the server title', () => {
		let ws = withTabs({ path: '/a.md', title: '/a.md' });
		ws = renameTab(ws, idOf(ws, '/a.md'), 'Spec')!;
		expect(ws.tabs[0].title).toBe('Spec');
	});
});

describe('restoreWorkspace', () => {
	it('returns every tab to unverified loading with its baseline intact', () => {
		const restored = restoreWorkspace({
			tabs: [
				{
					instanceId: 'i1',
					artifactId: 'art-1',
					path: '/a.md',
					title: 'a',
					loadedRevision: 'r1',
					latestRevision: 'r5',
					status: 'ready',
				},
			],
			activeTabId: 'i1',
			visible: true,
			activeSurface: 'artifact',
		})!;
		expect(restored.tabs[0].status).toBe('loading');
		expect(restored.tabs[0].latestRevision).toBeNull();
		expect(restored.tabs[0].loadedRevision).toBe('r1');
		expect(isTabChanged(restored.tabs[0])).toBe(false);
	});

	it('drops duplicates and repairs a dangling activeTabId', () => {
		const restored = restoreWorkspace({
			tabs: [
				{ instanceId: 'i1', artifactId: 'art-1', path: '/a.md', title: 'a' },
				{
					instanceId: 'i2',
					artifactId: 'art-1',
					path: '/moved/a.md',
					title: 'a',
				},
			],
			activeTabId: 'gone',
			visible: true,
			activeSurface: 'artifact',
		})!;
		expect(restored.tabs).toHaveLength(1);
		expect(restored.activeTabId).toBe('i1');
	});

	it('repairs a formerly compact background tab to the first active tab', () => {
		const restored = restoreWorkspace({
			tabs: [{ instanceId: 'i1', artifactId: 'art-1', path: '/a.md', title: 'a' }],
			activeTabId: null,
			visible: false,
			activeSurface: 'chat',
		})!;
		expect(restored.activeTabId).toBe('i1');
	});

	it('rejects unusable persisted shapes', () => {
		expect(restoreWorkspace(null)).toBeNull();
		expect(restoreWorkspace({ tabs: 'nope' })).toBeNull();
		expect(restoreWorkspace({ tabs: [] })).toBeNull();
	});
});
