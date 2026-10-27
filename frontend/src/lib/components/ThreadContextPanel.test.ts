import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import ThreadContextPanel from './ThreadContextPanel.svelte';
import { token } from '$lib/stores/auth';
import { activeThreadId, threads } from '$lib/stores/threads';
import { contextAttachTarget } from '$lib/stores/contextAttach';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { threadSettingsOpen } from '$lib/stores/threadSettings';
import { get } from 'svelte/store';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

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
	token.set('test-token');
	activeThreadId.set('thread-ctx');
	threads.set([
		{
			id: 'thread-ctx',
			user_id: 'steve',
			profile: 'default',
			title: 'CTX thread',
			model: 'anthropic/claude-sonnet-4-6',
			project_id: 'corsia',
			bootloader_files: ['/Users/alice/Documents/projects/corsia/thread-context.md'],
			created_at: '2026-07-02T00:00:00Z',
			updated_at: '2026-07-02T00:00:00Z',
		},
	]);
	contextAttachTarget.set(null);
	threadWorkspaces.set({});
	threadSettingsOpen.set(false);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
	activeThreadId.set(null);
	threads.set([]);
	contextAttachTarget.set(null);
	threadWorkspaces.set({});
	threadSettingsOpen.set(false);
});

describe('ThreadContextPanel', () => {
	it('edits a designated conductor absent from the visible thread list', async () => {
		threads.set([]);
		activeThreadId.set('conductor-1');
		const onHiddenThreadFilesChanged = vi.fn();
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			if (String(input) === '/api/context/bootloader' && init?.method === 'PATCH') {
				return { ok: true, json: async () => ({ files: [] }) };
			}
			throw new Error(`Unexpected fetch: ${input}`);
		});
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ThreadContextPanel, {
			target: host,
			props: {
				threadId: 'conductor-1',
				hiddenThread: { id: 'conductor-1', title: 'Conductor', project_id: 'corsia', bootloader_files: ['/project/CONTEXT.md'] },
				onHiddenThreadFilesChanged,
			},
		});
		flushSync();
		expect(host.textContent).toContain('CONTEXT.md');
		host.querySelector<HTMLButtonElement>('button[aria-label="Remove CONTEXT.md from chat context"]')?.click();
		await vi.waitFor(() => {
			expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
				scope: 'thread', thread_id: 'conductor-1', files: [],
			});
			expect(onHiddenThreadFilesChanged).toHaveBeenCalledWith([]);
		});
	});

	it('shows current thread bootloader files and removes injection without deleting the file', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url === '/api/context/bootloader' && init?.method === 'PATCH') {
				return { ok: true, json: async () => ({ ok: true, files: [] }) };
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ThreadContextPanel, { target: host, props: { threadId: 'thread-ctx' } });
		flushSync();

		expect(host.textContent).toContain('thread-context.md');
		host.querySelector<HTMLButtonElement>('button[aria-label="Remove thread-context.md from chat context"]')?.click();

		await vi.waitFor(() => {
			const call = fetchMock.mock.calls.find((c) => String(c[0]) === '/api/context/bootloader');
			expect(call).toBeTruthy();
			expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
				scope: 'thread',
				thread_id: 'thread-ctx',
				files: [],
			});
			expect(host.textContent).not.toContain('thread-context.md');
		});
	});

	it('creates a new thread context file through the shared bootloader API', async () => {
		const created = '/Users/alice/Documents/projects/corsia/working/thread-ctx/notes.md';
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url === '/api/context/bootloader/file' && init?.method === 'POST') {
				return { ok: true, json: async () => ({ ok: true, files: [created] }) };
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ThreadContextPanel, { target: host, props: { threadId: 'thread-ctx' } });
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Create file')
			?.click();
		flushSync();

		const input = host.querySelector<HTMLInputElement>('input[aria-label="New chat context file"]');
		expect(input).toBeTruthy();
		input!.value = 'notes.md';
		input!.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Create')
			?.click();

		await vi.waitFor(() => {
			const call = fetchMock.mock.calls.find((c) => String(c[0]) === '/api/context/bootloader/file');
			expect(call).toBeTruthy();
			expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
				scope: 'thread',
				thread_id: 'thread-ctx',
				path: 'notes.md',
				content: '# notes.md\n',
			});
			expect(host.textContent).toContain('notes.md');
		});
	});

	it('starts existing-file attach mode in the shared Documents browser', async () => {
		const onBrowseExisting = vi.fn();

		component = mount(ThreadContextPanel, {
			target: host,
			props: { threadId: 'thread-ctx', onBrowseExisting },
		});
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Add existing')
			?.click();

		await vi.waitFor(() => {
			expect(onBrowseExisting).toHaveBeenCalledTimes(1);
			expect(get(contextAttachTarget)).toEqual({
				scope: 'thread',
				threadId: 'thread-ctx',
				projectId: 'corsia',
				targetLabel: 'CTX thread',
				existingFiles: ['/Users/alice/Documents/projects/corsia/thread-context.md'],
				returnTo: 'thread-settings',
			});
		});
	});

	it('leaves thread settings when opening a pinned context file', () => {
		threadSettingsOpen.set(true);

		component = mount(ThreadContextPanel, { target: host, props: { threadId: 'thread-ctx' } });
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'thread-context.md')
			?.click();

		expect(get(threadSettingsOpen)).toBe(false);
		// The file opens as a tab in this thread, not in a standalone editor.
		const tabs = get(threadWorkspaces)['thread-ctx']?.tabs ?? [];
		expect(tabs).toHaveLength(1);
		expect(tabs[0]).toMatchObject({
			path: '/Users/alice/Documents/projects/corsia/thread-context.md',
			title: 'thread-context.md',
		});
	});
});
