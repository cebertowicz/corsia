import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { routeSubagentOpen, watchSubagentOpenRequests, type SubagentOpenDeps } from './subagentOpen';
import {
	_resetSubagentHydrationForTests, hydrateSubagentThread, openSubagentRequest, subagentMeta, type SubagentRef,
} from './stores/subagents';
import { threadWorkspaces } from './stores/artifactWorkspace';

function respondWith(body: unknown, status = 200): void {
	vi.stubGlobal('fetch', vi.fn(async () => ({
		ok: status < 400,
		status,
		json: async () => body,
	})) as never);
}

/** Routing against the real store and the real hydrator, with the server stubbed. */
function deps(): SubagentOpenDeps {
	return {
		readMeta: () => get(subagentMeta),
		hydrate: vi.fn((id: string) => hydrateSubagentThread(id, 'tok')),
		selectThread: vi.fn(),
		openLeaf: vi.fn(),
	};
}

beforeEach(() => {
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
});

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	subagentMeta.set(new Map());
	_resetSubagentHydrationForTests();
});

describe('routeSubagentOpen (#1949)', () => {
	it('opens a worker whose parent is known here as a leaf, without asking the server', async () => {
		subagentMeta.set(new Map([['child', { name: 'Worker', parentThreadId: 'parent' } as SubagentRef]]));
		const d = deps();

		await routeSubagentOpen('child', d);

		expect(d.hydrate).not.toHaveBeenCalled();
		expect(d.selectThread).toHaveBeenCalledWith('parent');
		expect(d.openLeaf).toHaveBeenCalledWith('parent', { threadId: 'child', title: 'Worker' });
	});

	it('opens a worker the page never registered as a leaf beside the parent the server names', async () => {
		respondWith({ thread_kind: 'subagent', parent_thread_id: 'parent', title: 'Codex worker' });
		const d = deps();

		await routeSubagentOpen('child', d);

		expect(d.selectThread).toHaveBeenCalledOnce();
		expect(d.selectThread).toHaveBeenCalledWith('parent');
		expect(d.openLeaf).toHaveBeenCalledWith('parent', { threadId: 'child', title: 'Codex worker' });
	});

	it('opens a thread the server says is not a worker normally', async () => {
		respondWith({ thread_kind: 'chat', parent_thread_id: null });
		const d = deps();

		await routeSubagentOpen('peer', d);

		expect(d.selectThread).toHaveBeenCalledWith('peer');
		expect(d.openLeaf).not.toHaveBeenCalled();
	});

	it('opens a visible thread with a lineage parent normally, never as a leaf', async () => {
		respondWith({ thread_kind: 'chat', parent_thread_id: 'conductor' });
		const d = deps();

		await routeSubagentOpen('coordinator', d);

		expect(d.selectThread).toHaveBeenCalledOnce();
		expect(d.selectThread).toHaveBeenCalledWith('coordinator');
		expect(d.openLeaf).not.toHaveBeenCalled();
	});

	it('opens a missing or unreadable thread as it always has', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		respondWith({ detail: 'Thread not found' }, 404);
		const d = deps();

		await routeSubagentOpen('gone', d);

		expect(d.selectThread).toHaveBeenCalledWith('gone');
		expect(d.openLeaf).not.toHaveBeenCalled();
	});
});

describe('watchSubagentOpenRequests', () => {
	it('consumes each request once and opens the leaf in the parent', async () => {
		subagentMeta.set(new Map([['child', {
			childThreadId: 'child', parentThreadId: 'parent', name: 'Worker', provider: 'codex', status: 'running',
		} as SubagentRef]]));
		const selectThread = vi.fn();
		const stop = watchSubagentOpenRequests({ selectThread });

		openSubagentRequest.set('child');
		await Promise.resolve();

		expect(get(openSubagentRequest)).toBeNull();
		expect(selectThread).toHaveBeenCalledOnce();
		expect(selectThread).toHaveBeenCalledWith('parent');
		expect(JSON.stringify(get(threadWorkspaces).parent)).toContain('child');
		stop();
	});
});
