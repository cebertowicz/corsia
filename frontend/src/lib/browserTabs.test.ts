import { describe, expect, it } from 'vitest';
import { openBrowserTab } from './artifactAuxiliaryTabs';
import { emptyWorkspace } from './artifactTabs';
import { restoreWorkspace } from './artifactWorkspaceRestore';

describe('browser artifact tabs', () => {
	it('opens one browser tab per thread', () => {
		let workspace = openBrowserTab(emptyWorkspace(), { threadId: 'thread-1' }, () => 'id');
		workspace = openBrowserTab(workspace, { threadId: 'thread-1' }, () => 'id');
		expect(workspace.tabs).toHaveLength(1);
		expect(workspace.tabs[0].kind).toBe('browser');
	});

	it('restores a browser tab for its owning thread', () => {
		const workspace = restoreWorkspace({ tabs: [{ kind: 'browser', instanceId: 'browser-1', threadId: 'thread-1' }], activeTabId: 'browser-1' }, () => 'new');
		expect(workspace?.tabs[0]).toMatchObject({ kind: 'browser', threadId: 'thread-1' });
	});

	it('adds an automatic tab without taking focus', () => {
		const first = openBrowserTab(emptyWorkspace(), { threadId: 'thread-1' }, () => 'id');
		const workspace = openBrowserTab(first, { threadId: 'thread-2' }, () => 'id-2', false);
		expect(workspace.activeTabId).toBe(first.activeTabId);
		expect(workspace.tabs).toHaveLength(2);
	});
});
