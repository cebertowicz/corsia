import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchRevision: vi.fn() }));

vi.mock('$lib/api/docs', () => ({ fetchRevision: mocks.fetchRevision }));

import { startArtifactRevisionWatch } from './artifactRevisionWatch';
import { emit } from '$lib/ws/events';
import { token } from '$lib/stores/auth';
import { threads } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';
import { subagentMeta } from '$lib/stores/subagents';
import {
	applyTabLoadSignal, markTabSaved, openArtifactTab, threadWorkspaces,
} from '$lib/stores/artifactWorkspace';

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((res) => { resolve = res; });
	return { promise, resolve };
}

function revision(rev: string) {
	return { exists: true, path: null, revision: rev, size: 1, writable: true, artifact_id: 'art-1' };
}

function openLoaded(threadId: string, artifactId: string, path: string, rev = 'r0'): string {
	openArtifactTab(threadId, { artifactId, path, title: path });
	const instanceId = get(threadWorkspaces)[threadId].tabs[0].instanceId;
	applyTabLoadSignal(threadId, {
		instanceId, status: 'ready', revision: rev, artifactId, writable: true,
	});
	return instanceId;
}

beforeEach(() => {
	localStorage.clear();
	threadWorkspaces.set({});
	subagentMeta.set(new Map());
	threads.set([
		{ id: 'thread-a', project_id: 'project-1' },
		{ id: 'thread-b', project_id: 'project-1' },
	] as never);
	uiScope.set({ id: 'project-1', name: 'Project 1', color: '#000' });
	token.set('tok');
	mocks.fetchRevision.mockReset();
});

afterEach(() => {
	token.set(null);
	uiScope.set(null);
	threads.set([]);
	subagentMeta.set(new Map());
	vi.restoreAllMocks();
});

describe('artifact revision watch', () => {
	it('probes only open copies of the artifact named by a changed hint', async () => {
		openLoaded('thread-a', 'art-1', '/a.md');
		openLoaded('thread-b', 'art-2', '/b.md');
		mocks.fetchRevision.mockResolvedValue(revision('r0'));
		const stop = startArtifactRevisionWatch();
		try {
			emit('artifacts_changed', { artifact_id: 'unrelated' });
			await Promise.resolve();
			expect(mocks.fetchRevision).not.toHaveBeenCalled();

			emit('artifacts_changed', { artifact_id: 'art-1' });
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(1));
			expect(mocks.fetchRevision).toHaveBeenCalledWith('/a.md', 'project-1', 'art-1');
		} finally {
			stop();
		}
	});

	it('probes every open tab across threads when the window regains focus', async () => {
		openLoaded('thread-a', 'art-1', '/a.md');
		openLoaded('thread-b', 'art-2', '/b.md');
		mocks.fetchRevision.mockResolvedValue(revision('r0'));
		const stop = startArtifactRevisionWatch();
		try {
			window.dispatchEvent(new Event('focus'));
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(2));
			expect(mocks.fetchRevision.mock.calls.map(([path]) => path).sort()).toEqual(['/a.md', '/b.md']);
		} finally {
			stop();
		}
	});

	it('coalesces focus and visibility restoration into one global probe burst', async () => {
		openLoaded('thread-a', 'art-1', '/a.md');
		openLoaded('thread-b', 'art-2', '/b.md');
		mocks.fetchRevision.mockResolvedValue(revision('r0'));
		const stop = startArtifactRevisionWatch();
		try {
			window.dispatchEvent(new Event('focus'));
			document.dispatchEvent(new Event('visibilitychange'));
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(2));
			await new Promise((resolve) => setTimeout(resolve, 0));
			expect(mocks.fetchRevision).toHaveBeenCalledTimes(2);
		} finally {
			stop();
		}
	});

	it('skips persisted tabs outside the current project scope', async () => {
		openLoaded('thread-a', 'art-1', '/a.md');
		openLoaded('thread-b', 'art-2', '/b.md');
		threads.set([
			{ id: 'thread-a', project_id: 'project-1' },
			{ id: 'thread-b', project_id: 'project-2' },
		] as never);
		mocks.fetchRevision.mockResolvedValue(revision('r0'));
		const stop = startArtifactRevisionWatch();
		try {
			window.dispatchEvent(new Event('focus'));
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(1));
			expect(mocks.fetchRevision).toHaveBeenCalledWith('/a.md', 'project-1', 'art-1');
		} finally {
			stop();
		}
	});

	it('probes a hidden subagent tab through its current-scope parent', async () => {
		openLoaded('child-1', 'art-1', '/child.md');
		subagentMeta.set(new Map([['child-1', {
			childThreadId: 'child-1', parentThreadId: 'thread-a', name: 'child', provider: 'cc', status: 'running',
		}]]));
		mocks.fetchRevision.mockResolvedValue(revision('r0'));
		const stop = startArtifactRevisionWatch();
		try {
			emit('artifacts_changed', { artifact_id: 'art-1' });
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledWith('/child.md', 'project-1', 'art-1'));
		} finally {
			stop();
		}
	});

	it('coalesces overlapping hints so the older probe cannot apply last', async () => {
		const instanceId = openLoaded('thread-a', 'art-1', '/a.md');
		const older = deferred<ReturnType<typeof revision>>();
		const newer = deferred<ReturnType<typeof revision>>();
		mocks.fetchRevision
			.mockImplementationOnce(() => older.promise)
			.mockImplementationOnce(() => newer.promise);
		const stop = startArtifactRevisionWatch();
		try {
			emit('artifacts_changed', { artifact_id: 'art-1' });
			emit('artifacts_changed', { artifact_id: 'art-1' });
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(1));
			older.resolve(revision('r1'));
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(2));
			newer.resolve(revision('r2'));
			await vi.waitFor(() => expect(
				get(threadWorkspaces)['thread-a'].tabs.find((tab) => tab.instanceId === instanceId)?.latestRevision,
			).toBe('r2'));
		} finally {
			stop();
		}
	});

	it('discards a probe that started before the same tab saved a newer baseline', async () => {
		const instanceId = openLoaded('thread-a', 'art-1', '/a.md');
		const beforeSave = deferred<ReturnType<typeof revision>>();
		mocks.fetchRevision.mockImplementationOnce(() => beforeSave.promise);
		const stop = startArtifactRevisionWatch();
		try {
			emit('artifacts_changed', { artifact_id: 'art-1' });
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(1));
			markTabSaved('thread-a', instanceId, 'r2');
			beforeSave.resolve(revision('r1'));
			await new Promise((resolve) => setTimeout(resolve, 0));
			const tab = get(threadWorkspaces)['thread-a'].tabs[0];
			expect(tab.loadedRevision).toBe('r2');
			expect(tab.latestRevision).toBe('r2');
		} finally {
			stop();
		}
	});

	it('keeps the saving copy clean while a matching open copy in another thread dots', async () => {
		const savingId = openLoaded('thread-a', 'art-1', '/a.md');
		const otherId = openLoaded('thread-b', 'art-1', '/a.md');
		markTabSaved('thread-a', savingId, 'r2');
		markTabSaved('thread-b', otherId, 'r0');
		mocks.fetchRevision.mockResolvedValue(revision('r2'));
		const stop = startArtifactRevisionWatch();
		try {
			emit('artifacts_changed', { artifact_id: 'art-1' });
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(2));
			const saving = get(threadWorkspaces)['thread-a'].tabs.find((tab) => tab.instanceId === savingId)!;
			const other = get(threadWorkspaces)['thread-b'].tabs.find((tab) => tab.instanceId === otherId)!;
			expect(saving.loadedRevision).toBe(saving.latestRevision);
			expect(other.loadedRevision).toBe('r0');
			expect(other.latestRevision).toBe('r2');
		} finally {
			stop();
		}
	});

	it('treats an unchanged hint as a no-op for the changed dot', async () => {
		const instanceId = openLoaded('thread-a', 'art-1', '/a.md', 'r1');
		mocks.fetchRevision.mockResolvedValue(revision('r1'));
		const stop = startArtifactRevisionWatch();
		try {
			emit('artifacts_changed', { artifact_id: 'art-1' });
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledTimes(1));
			expect(get(threadWorkspaces)['thread-a'].tabs.find((tab) => tab.instanceId === instanceId)?.latestRevision).toBe('r1');
		} finally {
			stop();
		}
	});
});
