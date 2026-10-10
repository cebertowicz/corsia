import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import DocumentsView from './DocumentsView.svelte';
import { contextAttachTarget } from '$lib/stores/contextAttach';
import { token } from '$lib/stores/auth';
import { threads } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';
import { get } from 'svelte/store';
import { docLens } from '$lib/stores/documentsSurface';

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
	uiScope.set({ id: 'corsia', name: 'CORSIA', color: '#7c3aed' });
	threads.set([
		{
			id: 'thread-ctx',
			user_id: 'steve',
			profile: 'default',
			title: 'CTX thread',
			model: 'anthropic/claude-sonnet-4-6',
			project_id: 'corsia',
			bootloader_files: [],
			created_at: '2026-07-02T00:00:00Z',
			updated_at: '2026-07-02T00:00:00Z',
		},
	]);
	contextAttachTarget.set({
		scope: 'thread',
		threadId: 'thread-ctx',
		projectId: 'corsia',
		targetLabel: 'CTX thread',
		existingFiles: [],
		returnTo: 'thread-settings',
	});
	docLens.set('all');
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
	uiScope.set(null);
	threads.set([]);
	contextAttachTarget.set(null);
	docLens.set('all');
});

describe('DocumentsView context attach mode', () => {
	it('counts the visible skill documents in the Skills lens chip', async () => {
		contextAttachTarget.set(null);
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.startsWith('/api/files/tree')) {
				return {
					ok: true,
					json: async () => ({
						roots: [
							{
								id: 'workspace',
								label: 'Workspace',
								kind: 'workspace',
								path: '/Users/alice/Documents/projects/corsia',
								capped: false,
								nodes: [
									{
										rel: 'skills/README.md',
										name: 'README.md',
										size: 100,
										mtime: 1782967512,
										tmp: false,
										artifact: null,
									},
									{
										rel: 'skills/ui-surface-review/SKILL.md',
										name: 'SKILL.md',
										size: 100,
										mtime: 1782967512,
										tmp: false,
										artifact: null,
									},
									{
										rel: 'skills/ui-surface-review/agents/openai.yaml',
										name: 'openai.yaml',
										size: 100,
										mtime: 1782967512,
										tmp: false,
										artifact: null,
									},
								],
							},
						],
					}),
				};
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(DocumentsView, { target: host });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('SKILL.md'));
		const skillsChip = Array.from(host.querySelectorAll<HTMLButtonElement>('button.chip'))
			.find((button) => button.textContent?.includes('Skills'));
		expect(skillsChip?.textContent?.replace(/\s+/g, '')).toBe('Skills3');
		skillsChip?.click();
		await vi.waitFor(() => expect(host.textContent).toContain('openai.yaml'));
		expect(host.textContent).toContain('skills/README.md');
		expect(host.textContent).toContain('skills/ui-surface-review/SKILL.md');
	});

	it('attaches a selected browser row to thread context through the shared bootloader API', async () => {
		const existing = '/Users/alice/Documents/projects/corsia/AGENTS.md';
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/files/tree')) {
				return {
					ok: true,
					json: async () => ({
						roots: [
							{
								id: 'workspace',
								label: 'Workspace',
								kind: 'workspace',
								path: '/Users/alice/Documents/projects/corsia',
								capped: false,
								nodes: [
									{
										rel: 'AGENTS.md',
										name: 'AGENTS.md',
										size: 100,
										mtime: 1782967512,
										tmp: false,
										artifact: null,
									},
								],
							},
						],
					}),
				};
			}
			if (url === '/api/context/bootloader/attach' && init?.method === 'POST') {
				return { ok: true, json: async () => ({ ok: true, files: [existing] }) };
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(DocumentsView, { target: host });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('AGENTS.md'));
		expect(host.textContent).toContain('Add to thread context: CTX thread');
		expect(host.textContent).not.toContain('＋ Add');
		const addButton = host.querySelector<HTMLButtonElement>('button[aria-label="Add AGENTS.md to thread context"]');
		expect(addButton?.tabIndex).toBe(0);
		expect(addButton?.classList.contains('row-attach')).toBe(true);
		addButton?.click();

		await vi.waitFor(() => {
			const call = fetchMock.mock.calls.find((c) => String(c[0]) === '/api/context/bootloader/attach');
			expect(call).toBeTruthy();
			expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
				scope: 'thread',
				thread_id: 'thread-ctx',
				path: existing,
			});
			expect(host.textContent).toContain('Add to thread context: CTX thread');
			expect(get(contextAttachTarget)?.returnTo).toBe('thread-settings');
			expect(get(contextAttachTarget)?.existingFiles).toEqual([existing]);
		});
		expect(host.querySelector<HTMLButtonElement>('button[aria-label="AGENTS.md is already in context"]')?.disabled).toBe(true);
	});

	it('only offers eligible unattached text files in context attach mode', async () => {
		const existing = '/Users/alice/Documents/projects/corsia/AGENTS.md';
		contextAttachTarget.set({
			scope: 'thread',
			threadId: 'thread-ctx',
			projectId: 'corsia',
			targetLabel: 'CTX thread',
			existingFiles: [existing],
			returnTo: 'thread-settings',
		});
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.startsWith('/api/files/tree')) {
				return {
					ok: true,
					json: async () => ({
						roots: [
							{
								id: 'workspace',
								label: 'Workspace',
								kind: 'workspace',
								path: '/Users/alice/Documents/projects/corsia',
								capped: false,
								nodes: [
									{
										rel: 'AGENTS.md',
										name: 'AGENTS.md',
										size: 100,
										mtime: 1782967512,
										tmp: false,
										artifact: null,
									},
									{
										rel: 'DESIGN-SYSTEM.md',
										name: 'DESIGN-SYSTEM.md',
										size: 100,
										mtime: 1782967512,
										tmp: false,
										artifact: null,
									},
									{
										rel: 'screenshot.png',
										name: 'screenshot.png',
										size: 100,
										mtime: 1782967512,
										tmp: false,
										artifact: null,
									},
								],
							},
						],
					}),
				};
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(DocumentsView, { target: host });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('DESIGN-SYSTEM.md'));
		expect(host.querySelector<HTMLButtonElement>('button[aria-label="AGENTS.md is already in context"]')?.disabled).toBe(true);
		expect(host.querySelector<HTMLButtonElement>('button[aria-label="Add DESIGN-SYSTEM.md to thread context"]')).toBeTruthy();
		expect(host.querySelector<HTMLButtonElement>('button[aria-label="Add screenshot.png to thread context"]')).toBeNull();
	});

	it('adds the owner to search and renders an Overdue badge only for overdue documents', async () => {
		contextAttachTarget.set(null);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (!String(input).startsWith('/api/files/tree')) throw new Error(`Unexpected fetch: ${String(input)}`);
			return {
				ok: true,
				json: async () => ({
					roots: [{
						id: 'workspace', label: 'Workspace', kind: 'workspace', path: '/workspace', capped: false,
						nodes: [
							{ rel: 'shared/overdue.md', name: 'overdue.md', size: 100, mtime: 1782967512, tmp: false, owner_name: 'Structured Plans', freshness: { state: 'overdue' }, artifact: { id: 'overdue', description: null, owner_thread_id: 'plans', owner_thread: 'Plans', updated_at: '2026-09-30T00:00:00Z' } },
							{ rel: 'shared/current.md', name: 'current.md', size: 100, mtime: 1782967512, tmp: false, owner_name: 'Documents & Artifacts', freshness: { state: 'current' }, artifact: { id: 'current', description: null, owner_thread_id: 'documents', owner_thread: 'Documents', updated_at: '2026-09-30T00:00:00Z' } },
						],
					}],
				}),
			};
		}));

		component = mount(DocumentsView, { target: host });
		flushSync();
		await vi.waitFor(() => expect(host.textContent).toContain('Owner: Structured Plans'));
		expect(host.textContent).toContain('Overdue');
		expect(host.textContent).not.toContain('Owner: Documents & ArtifactsOverdue');

		const search = host.querySelector<HTMLInputElement>('input[placeholder="Search docs…"]')!;
		search.value = 'structured plans';
		search.dispatchEvent(new Event('input', { bubbles: true }));
		await vi.waitFor(() => expect(host.textContent).toContain('overdue.md'));
		expect(host.textContent).not.toContain('current.md');
	});
});
