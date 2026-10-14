import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
	threadWorkspaces, splitWidth, emptyWorkspace, getActiveTab,
	openArtifactTab, openAgentDocumentTab, openBrowserPane, openSubagentConversationTab, openLegacyDiscussionArchiveTab,
	closeArtifactTab, setActiveTab, setActiveSurface, setLeafVisible, markTabStatus,
} from './artifactWorkspace';
import { token } from './auth';
import { canvasCompact } from './artifactCanvas';
import { existingDraft, seedDraft, setDraftText } from './docSaveQueue';

const STORAGE_KEY = 'corsia:artifactWorkspace:v1';

beforeEach(() => {
	localStorage.clear();
	threadWorkspaces.set({});
	splitWidth.set(null);
	canvasCompact.set(false);
	token.set('account-a');
});

/** Tab-instance key for the tab currently holding this artifact id. */
function keyOf(threadId: string, artifactId: string): string {
	const tab = get(threadWorkspaces)[threadId]?.tabs.find((t) => t.artifactId === artifactId);
	if (!tab) throw new Error(`no tab for ${artifactId}`);
	return tab.instanceId;
}

describe('openBrowserPane', () => {
	it('foregrounds and reveals a newly agent-opened browser tab', () => {
		openBrowserPane('thread-1', true);
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws).toMatchObject({ visible: true, activeSurface: 'artifact', activeTabId: ws.tabs[0].instanceId });
	});

	it('does not interrupt the tab or surface after an agent reopens its browser', () => {
		openBrowserPane('thread-1', true);
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		const before = get(threadWorkspaces)['thread-1'];
		openBrowserPane('thread-1', true);
		const after = get(threadWorkspaces)['thread-1'];
		expect(after.activeTabId).toBe(before.activeTabId);
		expect(after.activeSurface).toBe(before.activeSurface);
	});
});

describe('openArtifactTab', () => {
	it('reveals and selects an agent-requested document without switching the chat surface', () => {
		openAgentDocumentTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });

		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.activeTabId).toBe(ws.tabs[0].instanceId);
		expect(ws.visible).toBe(true);
		expect(ws.activeSurface).toBe('chat');
	});

	it('adds a tab, reveals the leaf, and switches activeSurface to artifact', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs).toHaveLength(1);
		expect(ws.tabs[0]).toMatchObject({ artifactId: 'art-1', path: '/docs/a.md', title: 'a.md', status: 'loading' });
		expect(ws.activeTabId).toBe(ws.tabs[0].instanceId);
		expect(ws.visible).toBe(true);
		expect(ws.activeSurface).toBe('artifact');
	});

	it('gives every tab its own instance key, independent of artifact identity', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		openArtifactTab('thread-1', { path: '/docs/b.md', title: 'b.md' });
		const ws = get(threadWorkspaces)['thread-1'];
		const ids = ws.tabs.map((t) => t.instanceId);
		expect(new Set(ids).size).toBe(2);
		expect(ids).not.toContain('art-1');
	});

	it('dedupes by artifactId — reopening selects the existing tab instead of duplicating', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		openArtifactTab('thread-1', { artifactId: 'art-2', path: '/docs/b.md', title: 'b.md' });
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs).toHaveLength(2);
		expect(ws.activeTabId).toBe(keyOf('thread-1', 'art-1'));
	});

	it('dedupes by artifactId even when the document has since moved', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/archive/a.md', title: 'a.md' });
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs).toHaveLength(1);
		expect(ws.tabs[0].path).toBe('/docs/a.md');
	});

	it('dedupes by path when no artifactId is known yet', () => {
		openArtifactTab('thread-1', { path: '/docs/unregistered.md', title: 'unregistered.md' });
		openArtifactTab('thread-1', { path: '/docs/unregistered.md', title: 'unregistered.md' });
		expect(get(threadWorkspaces)['thread-1'].tabs).toHaveLength(1);
	});

	it('enriches a path-only tab when the same document is reopened with its artifactId', () => {
		openArtifactTab('thread-1', { path: '/docs/unregistered.md', title: 'unregistered.md' });
		const before = get(threadWorkspaces)['thread-1'].tabs[0].instanceId;
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/unregistered.md', title: 'unregistered.md' });
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs).toHaveLength(1);
		expect(ws.tabs[0].artifactId).toBe('art-1');
		// Enrichment must not move the key out from under the tab's draft.
		expect(ws.tabs[0].instanceId).toBe(before);
		expect(ws.activeTabId).toBe(before);
	});

	it('keeps the same artifact independently open across different threads', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		openArtifactTab('thread-2', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		expect(get(threadWorkspaces)['thread-1'].tabs).toHaveLength(1);
		expect(get(threadWorkspaces)['thread-2'].tabs).toHaveLength(1);
		closeArtifactTab('thread-1', keyOf('thread-1', 'art-1'));
		expect(get(threadWorkspaces)['thread-1'].tabs).toHaveLength(0);
		expect(get(threadWorkspaces)['thread-2'].tabs).toHaveLength(1);
	});
});

describe('closeArtifactTab', () => {
	it('selects a neighbor when the active tab is closed', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		openArtifactTab('thread-1', { artifactId: 'art-2', path: '/b.md', title: 'b.md' });
		openArtifactTab('thread-1', { artifactId: 'art-3', path: '/c.md', title: 'c.md' });
		setActiveTab('thread-1', keyOf('thread-1', 'art-2'));
		closeArtifactTab('thread-1', keyOf('thread-1', 'art-2'));
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs.map((t) => t.artifactId)).toEqual(['art-1', 'art-3']);
		expect(ws.activeTabId).toBe(keyOf('thread-1', 'art-3'));
	});

	it('returns to chat-only when the last tab closes', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		closeArtifactTab('thread-1', keyOf('thread-1', 'art-1'));
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs).toHaveLength(0);
		expect(ws.activeTabId).toBeNull();
		expect(ws.visible).toBe(false);
		expect(ws.activeSurface).toBe('chat');
	});

	it('preserves tabs and visibility when a non-active tab closes', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		openArtifactTab('thread-1', { artifactId: 'art-2', path: '/b.md', title: 'b.md' });
		setActiveTab('thread-1', keyOf('thread-1', 'art-1'));
		closeArtifactTab('thread-1', keyOf('thread-1', 'art-2'));
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs.map((t) => t.artifactId)).toEqual(['art-1']);
		expect(ws.activeTabId).toBe(keyOf('thread-1', 'art-1'));
		expect(ws.visible).toBe(true);
	});
});

describe('activeSurface rules', () => {
	it('setActiveSurface("artifact") is a no-op when there are no tabs', () => {
		setActiveSurface('thread-1', 'artifact');
		expect(get(threadWorkspaces)['thread-1']).toBeUndefined();
	});

	it('setActiveSurface flips to chat and back to artifact', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		setActiveSurface('thread-1', 'chat');
		expect(get(threadWorkspaces)['thread-1'].activeSurface).toBe('chat');
		setActiveSurface('thread-1', 'artifact');
		expect(get(threadWorkspaces)['thread-1'].activeSurface).toBe('artifact');
	});

	it('selecting a tab switches activeSurface to artifact', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		openArtifactTab('thread-1', { artifactId: 'art-2', path: '/b.md', title: 'b.md' });
		setActiveSurface('thread-1', 'chat');
		setActiveTab('thread-1', keyOf('thread-1', 'art-1'));
		expect(get(threadWorkspaces)['thread-1'].activeSurface).toBe('artifact');
	});
});

describe('collapse / rail visibility', () => {
	it('setLeafVisible toggles collapse without dropping tabs', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		setLeafVisible('thread-1', false);
		let ws = get(threadWorkspaces)['thread-1'];
		expect(ws.visible).toBe(false);
		expect(ws.tabs).toHaveLength(1);
		setLeafVisible('thread-1', true);
		ws = get(threadWorkspaces)['thread-1'];
		expect(ws.visible).toBe(true);
	});

	it('reveals an existing requested tab without overwriting an inactive dirty draft', () => {
		openArtifactTab('thread-1', { artifactId: 'art-a', path: '/a.md', title: 'a.md' });
		const dirtyId = keyOf('thread-1', 'art-a');
		seedDraft({ instanceId: dirtyId, artifactId: 'art-a', path: '/a.md', name: 'a.md', threadId: 'thread-1', projectId: null }, 'saved');
		setDraftText({ instanceId: dirtyId }, 'unsaved A');
		openArtifactTab('thread-1', { artifactId: 'art-b', path: '/b.md', title: 'b.md' });
		const requestedId = keyOf('thread-1', 'art-b');
		setLeafVisible('thread-1', false);
		setActiveSurface('thread-1', 'chat');

		openAgentDocumentTab('thread-1', { artifactId: 'art-b', path: '/b.md', title: 'b.md' });

		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs.map((tab) => tab.artifactId)).toEqual(['art-a', 'art-b']);
		expect(ws.activeTabId).toBe(requestedId);
		expect(ws.visible).toBe(true);
		expect(ws.activeSurface).toBe('chat');
		expect(existingDraft({ instanceId: dirtyId })).toMatchObject({ editText: 'unsaved A', savedText: 'saved', status: 'dirty' });
	});
});

describe('markTabStatus', () => {
	it('updates only the targeted tab and is a no-op when unchanged', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		openArtifactTab('thread-1', { artifactId: 'art-2', path: '/b.md', title: 'b.md' });
		markTabStatus('thread-1', keyOf('thread-1', 'art-1'), 'ready');
		const ws = get(threadWorkspaces)['thread-1'];
		expect(ws.tabs.find((t) => t.artifactId === 'art-1')?.status).toBe('ready');
		expect(ws.tabs.find((t) => t.artifactId === 'art-2')?.status).toBe('loading');

		const before = get(threadWorkspaces);
		markTabStatus('thread-1', keyOf('thread-1', 'art-1'), 'ready');
		expect(get(threadWorkspaces)).toBe(before); // no-op — same reference, no redundant notify
	});
});

describe('getActiveTab', () => {
	it('resolves the tab matching activeTabId', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		const ws = get(threadWorkspaces)['thread-1'];
		expect(getActiveTab(ws)?.artifactId).toBe('art-1');
	});

	it('returns null for an empty workspace', () => {
		expect(getActiveTab(emptyWorkspace())).toBeNull();
	});
});

describe('device-local persistence', () => {
	it('keeps auxiliary child and discussion identities session-only', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		openSubagentConversationTab('thread-1', {
			threadId: 'child-secret', title: 'Child secret', sourceNodeId: 'node-1',
		});
		openLegacyDiscussionArchiveTab('thread-1', {
			artifactId: 'discussion-secret', title: 'Discussion secret',
		});
		const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
		expect(parsed.threads['thread-1'].tabs.map((tab: { kind: string }) => tab.kind)).toEqual([
			'document',
		]);
	});

	it('removes in-memory auxiliary identities when the account token changes', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		openSubagentConversationTab('thread-1', {
			threadId: 'child-secret', title: 'Child secret', sourceNodeId: 'node-1',
		});
		openLegacyDiscussionArchiveTab('thread-1', {
			artifactId: 'discussion-secret', title: 'Discussion secret',
		});
		token.set('account-b');
		expect(get(threadWorkspaces)['thread-1'].tabs.map((tab) => tab.kind)).toEqual([
			'document',
		]);
	});

	it('writes thread workspaces and split width to localStorage', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		splitWidth.set(480);
		const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
		expect(parsed.threads['thread-1'].tabs[0].artifactId).toBe('art-1');
		expect(parsed.threads['thread-1'].tabs[0].instanceId).toBeTruthy();
		expect(parsed.splitWidth).toBe(480);
	});

	it('rehydrates tabs, enriched identity, and split width on a fresh module load', async () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/a.md', title: 'a.md' });
		setActiveSurface('thread-1', 'chat');
		splitWidth.set(555);
		const instanceId = keyOf('thread-1', 'art-1');

		vi.resetModules();
		const fresh = await import('./artifactWorkspace');
		const ws = get(fresh.threadWorkspaces)['thread-1'];
		expect(ws.tabs).toHaveLength(1);
		expect(ws.tabs[0].artifactId).toBe('art-1');
		expect(ws.tabs[0].instanceId).toBe(instanceId);
		expect(ws.activeSurface).toBe('chat');
		expect(get(fresh.splitWidth)).toBe(555);
	});

	it('restores pre-instance-key tabs by adopting their artifact identity as the key', async () => {
		localStorage.setItem(STORAGE_KEY, JSON.stringify({
			threads: {
				'thread-1': {
					tabs: [{ artifactId: 'art-1', path: '/a.md', title: 'a.md', loadedRevision: '7', status: 'ready' }],
					activeTabId: 'art-1',
					visible: true,
					activeSurface: 'artifact',
				},
			},
			splitWidth: null,
		}));
		vi.resetModules();
		const fresh = await import('./artifactWorkspace');
		const ws = get(fresh.threadWorkspaces)['thread-1'];
		expect(ws.tabs[0].instanceId).toBe('art-1');
		expect(ws.activeTabId).toBe('art-1');
		expect(ws.tabs[0].loadedRevision).toBe('7');
	});

	it('does not restore auxiliary identities written by an older build', async () => {
		localStorage.setItem(STORAGE_KEY, JSON.stringify({
			threads: {
				'thread-1': {
					tabs: [
						{ kind: 'subagent', instanceId: 'sub', threadId: 'child-secret', title: 'Child secret' },
						{ kind: 'legacy-discussion', instanceId: 'discussion', artifactId: 'discussion-secret', title: 'Discussion secret' },
						{ kind: 'document', instanceId: 'doc', artifactId: 'art-1', path: '/a.md', title: 'a.md' },
					],
					activeTabId: 'sub',
					visible: true,
					activeSurface: 'artifact',
				},
			},
			splitWidth: null,
		}));
		vi.resetModules();
		const fresh = await import('./artifactWorkspace');
		const ws = get(fresh.threadWorkspaces)['thread-1'];
		expect(ws.tabs.map((tab) => tab.kind)).toEqual(['document']);
		expect(ws.activeTabId).toBe('doc');
	});

	it('falls back to empty state for corrupt localStorage content', async () => {
		localStorage.setItem(STORAGE_KEY, '{not json');
		vi.resetModules();
		const fresh = await import('./artifactWorkspace');
		expect(get(fresh.threadWorkspaces)).toEqual({});
		expect(get(fresh.splitWidth)).toBeNull();
	});
});
