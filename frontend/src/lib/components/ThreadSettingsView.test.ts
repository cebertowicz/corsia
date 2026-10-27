import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import ThreadSettingsView from './ThreadSettingsView.svelte';
import { activeThreadId, threads, type Thread } from '$lib/stores/threads';
import { threadSettingsOpen } from '$lib/stores/threadSettings';
import { fetchScopeTarget } from '$lib/utils/conductor';
import { uiScope } from '$lib/stores/uiScope';

vi.mock('$lib/utils/conductor', () => ({ fetchScopeTarget: vi.fn() }));

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const baseThread: Thread = {
	id: 'thread-settings',
	user_id: 'steve',
	profile: 'default',
	title: 'Thread settings',
	model: 'anthropic/claude-sonnet-4-6',
	created_at: '2026-07-02T00:00:00Z',
	updated_at: '2026-07-02T00:00:00Z',
};

beforeAll(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(),
			cancel: vi.fn(),
			commitStyles: vi.fn(),
			onfinish: null,
			playState: 'finished',
		})) as unknown as Element['animate'];
	}
});

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	threadSettingsOpen.set(true);
	activeThreadId.set(baseThread.id);
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.startsWith('/api/skills')) {
			return { ok: true, json: async () => ({ skills: [], root: '/workspace/working/thread-settings', scope_id: 'thread-settings' }) };
		}
		throw new Error(`Unexpected fetch: ${url}`);
	}));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	threadSettingsOpen.set(false);
	activeThreadId.set(null);
	threads.set([]);
});

describe('ThreadSettingsView', () => {
	it('shows workspace Agent promotion for an unprojected thread', () => {
		threads.set([{ ...baseThread, project_id: null }]);

		component = mount(ThreadSettingsView, { target: host });
		flushSync();

		expect(host.querySelector('[role="dialog"]')).toBeNull();
		expect(host.textContent).toContain('Context files');
		expect(host.textContent).toContain('Thread skills');
		expect(host.textContent).toContain('Promote this chat to a workspace agent when it has a clear responsibility.');
		expect(host.textContent).toContain('Make Agent');
		expect(host.textContent).not.toContain('Attach this thread to a project');
	});

	it('shows Agent controls for project-scoped threads', () => {
		threads.set([{ ...baseThread, project_id: 'corsia' }]);

		component = mount(ThreadSettingsView, { target: host });
		flushSync();

		expect(host.querySelector('[role="dialog"]')).toBeNull();
		expect(host.textContent).toContain('Context files');
		expect(host.textContent).toContain('Managing skills for Thread: Thread settings');
		expect(host.textContent).toContain('Promote this chat to a project agent when it has a clear responsibility.');
		expect(host.textContent).toContain('Make Agent');
	});

	describe('conductor mode', () => {
		const target = { thread_id: 'conductor-1', title: 'Conductor', project_id: 'corsia', bootloader_files: [] };
		beforeEach(() => {
			vi.mocked(fetchScopeTarget).mockReset();
			uiScope.set({ id: 'corsia', name: 'CORSIA', color: '#d65a87' });
		});
		afterEach(() => uiScope.set(null));
		const flush = async () => { await Promise.resolve(); await Promise.resolve(); flushSync(); };

		it('shows a loading state, then context and skills without the Agent section', async () => {
			activeThreadId.set('conductor-1');
			let resolve!: (lookup: { kind: 'target'; target: typeof target }) => void;
			vi.mocked(fetchScopeTarget).mockReturnValue(new Promise((r) => { resolve = r; }) as never);

			component = mount(ThreadSettingsView, { target: host, props: { conductor: true } });
			flushSync();
			expect(host.textContent).toContain('Loading conductor settings');
			expect(host.textContent).not.toContain('Context files');

			resolve({ kind: 'target', target });
			await flush();
			expect(host.textContent).toContain('Context files');
			expect(host.textContent).toContain('Thread: Conductor');
			expect(host.textContent).not.toContain('Make Agent');
		});

		it('names the workspace scope\'s designated thread CORSIA, not Conductor', async () => {
			uiScope.set(null);
			activeThreadId.set('agent-1');
			vi.mocked(fetchScopeTarget).mockResolvedValue({
				kind: 'target',
				target: { thread_id: 'agent-1', title: null, project_id: null, bootloader_files: [] },
			} as never);

			component = mount(ThreadSettingsView, { target: host, props: { conductor: true } });
			await flush();
			expect(host.textContent).toContain('Thread: CORSIA');
			expect(host.textContent).not.toContain('Conductor');
		});

		it('shows the unavailable state when the fetch returns nothing', async () => {
			activeThreadId.set('conductor-1');
			vi.mocked(fetchScopeTarget).mockResolvedValue({ kind: 'unknown' } as never);

			component = mount(ThreadSettingsView, { target: host, props: { conductor: true } });
			await flush();
			expect(host.textContent).toContain('Conductor settings unavailable');
			expect(host.textContent).not.toContain('Context files');
		});

		it('treats a conductor for a different thread as unavailable', async () => {
			activeThreadId.set('some-other-thread');
			vi.mocked(fetchScopeTarget).mockResolvedValue({ kind: 'target', target } as never);

			component = mount(ThreadSettingsView, { target: host, props: { conductor: true } });
			await flush();
			expect(host.textContent).toContain('Conductor settings unavailable');
		});

		it('refetches when the active thread changes', async () => {
			activeThreadId.set('conductor-1');
			vi.mocked(fetchScopeTarget).mockResolvedValue({ kind: 'target', target } as never);

			component = mount(ThreadSettingsView, { target: host, props: { conductor: true } });
			await flush();
			expect(fetchScopeTarget).toHaveBeenCalledTimes(1);

			activeThreadId.set('conductor-2');
			await flush();
			expect(fetchScopeTarget).toHaveBeenCalledTimes(2);
			expect(host.textContent).toContain('Conductor settings unavailable');
		});
	});
});
