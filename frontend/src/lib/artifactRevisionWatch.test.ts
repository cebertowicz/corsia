import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ fetchRevision: vi.fn() }));

vi.mock('$lib/api/docs', () => ({ fetchRevision: mocks.fetchRevision }));

import { checkThreadRevisions, startArtifactRevisionWatch } from './artifactRevisionWatch';
import { emit } from '$lib/ws/events';
import { COMMENT_POST_TIMEOUT_MS } from '$lib/api/boundedRequest';
import { token } from '$lib/stores/auth';
import { threads } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';
import { subagentMeta } from '$lib/stores/subagents';
import { activeThreadId } from '$lib/stores/activeThreadId';
import {
	applyTabLoadSignal, markTabSaved, openArtifactTab, openLinkLoadingTab, threadWorkspaces,
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
			expect(mocks.fetchRevision).toHaveBeenCalledWith('/a.md', 'project-1', 'art-1', COMMENT_POST_TIMEOUT_MS);
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

	it('never probes a link tab still resolving, on focus or on a chat switch', async () => {
		openLoaded('thread-a', 'art-1', '/a.md');
		const linkTab = openLinkLoadingTab('thread-a', { href: 'shared/x.md', projectId: 'project-1' }, 'x.md');
		mocks.fetchRevision.mockResolvedValue(revision('r0'));
		const stop = startArtifactRevisionWatch();
		try {
			window.dispatchEvent(new Event('focus'));
			checkThreadRevisions('thread-a');
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalled());
			await new Promise((resolve) => setTimeout(resolve, 0));
			expect(mocks.fetchRevision.mock.calls.map(([path]) => path)).not.toContain('');
			const tab = get(threadWorkspaces)['thread-a'].tabs.find((entry) => entry.instanceId === linkTab);
			expect(tab?.status).toBe('loading');
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
			expect(mocks.fetchRevision).toHaveBeenCalledWith('/a.md', 'project-1', 'art-1', COMMENT_POST_TIMEOUT_MS);
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
			await vi.waitFor(() => expect(mocks.fetchRevision).toHaveBeenCalledWith('/child.md', 'project-1', 'art-1', COMMENT_POST_TIMEOUT_MS));
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

	describe('background probes share two connections', () => {
		// Safari holds six connections per origin; background probes must leave
		// room for what the person just asked for (a comment post, a link press).
		interface Held { path: string; resolve: (rev: string) => void }
		let held: Held[];
		let inFlight: number;
		let peak: number;

		function holdEveryProbe(): void {
			held = [];
			inFlight = 0;
			peak = 0;
			mocks.fetchRevision.mockImplementation((path: string, _projectId: string | null, artifactId: string | null) => {
				inFlight += 1;
				peak = Math.max(peak, inFlight);
				return new Promise((resolve) => {
					held.push({
						path,
						resolve: (rev) => {
							inFlight -= 1;
							resolve({ ...revision(rev), artifact_id: artifactId });
						},
					});
				});
			});
		}

		async function settle(): Promise<void> {
			for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
		}

		async function releaseAll(rev = 'r1'): Promise<void> {
			for (let guard = 0; guard < 50; guard += 1) {
				await settle();
				const next = held.shift();
				if (!next) return;
				next.resolve(rev);
			}
		}

		function open(threadId: string, artifactId: string, path: string): string {
			openArtifactTab(threadId, { artifactId, path, title: path });
			const tab = get(threadWorkspaces)[threadId].tabs.find((entry) => entry.path === path)!;
			applyTabLoadSignal(threadId, {
				instanceId: tab.instanceId, status: 'ready', revision: 'r0', artifactId, writable: true,
			});
			return tab.instanceId;
		}

		function tabOf(threadId: string, instanceId: string) {
			return get(threadWorkspaces)[threadId].tabs.find((tab) => tab.instanceId === instanceId)!;
		}

		beforeEach(() => holdEveryProbe());
		afterEach(() => activeThreadId.set(null));

		it('runs a foreground sweep two at a time, the visible tab first, and checks every tab', async () => {
			open('thread-a', 'art-a1', '/a1.md');
			open('thread-a', 'art-a2', '/a2.md');
			open('thread-a', 'art-a3', '/a3.md');
			open('thread-b', 'art-b1', '/b1.md');
			const visible = open('thread-b', 'art-b2', '/b2.md');
			expect(get(threadWorkspaces)['thread-b'].activeTabId).toBe(visible);
			activeThreadId.set('thread-b');
			const stop = startArtifactRevisionWatch();
			try {
				window.dispatchEvent(new Event('focus'));
				await settle();
				expect(mocks.fetchRevision.mock.calls[0][0]).toBe('/b2.md');
				expect(inFlight).toBe(2);
				await releaseAll();
				expect(peak).toBe(2);
				expect(mocks.fetchRevision.mock.calls.map(([path]) => path).sort())
					.toEqual(['/a1.md', '/a2.md', '/a3.md', '/b1.md', '/b2.md']);
			} finally {
				stop();
			}
		});

		it('probes a file open in two threads once per sweep and updates both tabs', async () => {
			const inA = open('thread-a', 'art-1', '/a.md');
			const inB = open('thread-b', 'art-1', '/a.md');
			const stop = startArtifactRevisionWatch();
			try {
				window.dispatchEvent(new Event('focus'));
				await releaseAll('r2');
				await settle();
				expect(mocks.fetchRevision).toHaveBeenCalledTimes(1);
				expect(tabOf('thread-a', inA).latestRevision).toBe('r2');
				expect(tabOf('thread-b', inB).latestRevision).toBe('r2');
			} finally {
				stop();
			}
		});

		it('keeps a changed hint during a sweep inside the limit without losing its trailing check', async () => {
			open('thread-a', 'art-1', '/a.md');
			const visible = open('thread-a', 'art-2', '/b.md');
			open('thread-b', 'art-3', '/c.md');
			activeThreadId.set('thread-a');
			const stop = startArtifactRevisionWatch();
			try {
				window.dispatchEvent(new Event('focus'));
				await settle();
				expect(held.map((probe) => probe.path)).toEqual(['/b.md', '/a.md']);
				// /b.md is in flight, so its hint needs a second look; /c.md has not
				// started, so its coming probe already answers its hint.
				emit('artifacts_changed', { artifact_id: 'art-2' });
				emit('artifacts_changed', { artifact_id: 'art-3' });
				await settle();
				expect(inFlight).toBe(2);
				let answered = 0;
				for (let guard = 0; guard < 50; guard += 1) {
					await settle();
					const next = held.shift();
					if (!next) break;
					answered += 1;
					next.resolve(`r${answered}`);
				}
				expect(peak).toBe(2);
				const calls = mocks.fetchRevision.mock.calls.map(([path]) => path);
				expect(calls.filter((path) => path === '/b.md')).toHaveLength(2);
				expect(calls.filter((path) => path === '/a.md')).toHaveLength(1);
				expect(calls.filter((path) => path === '/c.md')).toHaveLength(1);
				expect(tabOf('thread-a', visible).latestRevision).toBe('r4');
			} finally {
				stop();
			}
		});

		it('gives a hint its own look when the tab saved while its probe waited for a slot', async () => {
			open('thread-a', 'art-1', '/a.md');
			open('thread-a', 'art-2', '/b.md');
			const waiting = open('thread-b', 'art-3', '/c.md');
			const stop = startArtifactRevisionWatch();
			try {
				window.dispatchEvent(new Event('focus'));
				await settle();
				expect(held.map((probe) => probe.path)).toEqual(['/a.md', '/b.md']);
				// The waiting probe's answer will be for the old baseline, so the
				// hint it absorbs must still get a look against the new one.
				markTabSaved('thread-b', waiting, 'r5');
				emit('artifacts_changed', { artifact_id: 'art-3' });
				await releaseAll('r6');
				const calls = mocks.fetchRevision.mock.calls.map(([path]) => path);
				expect(calls.filter((path) => path === '/c.md')).toHaveLength(2);
				expect(tabOf('thread-b', waiting).latestRevision).toBe('r6');
			} finally {
				stop();
			}
		});

		it('sends a waiting probe with the project its tab was checked in, after a project switch', async () => {
			open('thread-a', 'art-1', '/a.md');
			open('thread-a', 'art-2', '/b.md');
			open('thread-b', 'art-3', '/c.md');
			const stop = startArtifactRevisionWatch();
			try {
				window.dispatchEvent(new Event('focus'));
				await settle();
				expect(held.map((probe) => probe.path)).toEqual(['/a.md', '/b.md']);
				uiScope.set({ id: 'project-2', name: 'Project 2', color: '#000' });
				await releaseAll();
				const waited = mocks.fetchRevision.mock.calls.find(([path]) => path === '/c.md');
				expect(waited?.[1]).toBe('project-1');
			} finally {
				stop();
			}
		});

		it('frees the slots of two probes that never answer, so a later sweep still checks', async () => {
			vi.useFakeTimers();
			try {
				const first = open('thread-a', 'art-1', '/a.md');
				open('thread-a', 'art-2', '/b.md');
				// A request that never answers ends at the limit it is given, as readWithin ends it.
				mocks.fetchRevision.mockImplementation((_path: string, _projectId: string | null, _artifactId: string | null, within?: number) => (
					new Promise((_, reject) => {
						if (within) setTimeout(() => reject(new Error('timed out')), within);
					})
				));
				const stop = startArtifactRevisionWatch();
				try {
					window.dispatchEvent(new Event('focus'));
					await vi.advanceTimersByTimeAsync(0);
					expect(mocks.fetchRevision).toHaveBeenCalledTimes(2);
					await vi.advanceTimersByTimeAsync(COMMENT_POST_TIMEOUT_MS);
					mocks.fetchRevision.mockImplementation((_path: string, _projectId: string | null, artifactId: string | null) => (
						Promise.resolve({ ...revision('r1'), artifact_id: artifactId })
					));
					window.dispatchEvent(new Event('focus'));
					await vi.advanceTimersByTimeAsync(0);
					expect(mocks.fetchRevision).toHaveBeenCalledTimes(4);
					expect(tabOf('thread-a', first).latestRevision).toBe('r1');
				} finally {
					stop();
				}
			} finally {
				vi.useRealTimers();
			}
		});

		it('starts the next watch with every slot free when the last one stopped with probes hung', async () => {
			open('thread-a', 'art-1', '/a.md');
			open('thread-a', 'art-2', '/b.md');
			open('thread-b', 'art-3', '/c.md');
			let stop = startArtifactRevisionWatch();
			try {
				window.dispatchEvent(new Event('focus'));
				await settle();
				expect(held.map((probe) => probe.path)).toEqual(['/a.md', '/b.md']);
				stop();
				stop = startArtifactRevisionWatch();
				emit('artifacts_changed', { artifact_id: 'art-3' });
				await settle();
				expect(mocks.fetchRevision.mock.calls.map(([path]) => path)).toContain('/c.md');
			} finally {
				stop();
				await releaseAll();
			}
		});
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
