import { mount, unmount } from 'svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import ProjectSettingsView from './ProjectSettingsView.svelte';
import { token } from '$lib/stores/auth';
import { uiScope } from '$lib/stores/uiScope';
import { contextAttachTarget } from '$lib/stores/contextAttach';
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
	uiScope.set({ id: 'corsia', name: 'CORSIA', color: '#16a34a' });
	contextAttachTarget.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	uiScope.set(null);
	token.set(null);
	contextAttachTarget.set(null);
});

describe('ProjectSettingsView', () => {
	it('surfaces and repairs the project scope root from project settings', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/projects?')) {
				throw new Error('The project picker must not infer its root from sibling projects');
			}
			if (url.startsWith('/api/projects/corsia/repos')) {
				return { ok: true, json: async () => ({ repos: [] }) };
			}
			if (url.startsWith('/api/skills')) {
				return { ok: true, json: async () => ({ skills: [], root: '/workspace/projects/corsia', scope_id: 'corsia' }) };
			}
			if (url.startsWith('/api/projects/corsia') && init?.method === 'PATCH') {
				return {
					ok: true,
					json: async () => ({
						ok: true,
						project: {
							id: 'corsia',
							name: 'CORSIA',
							description: '',
							status: 'active',
							accent_color: null,
							path: '/workspace/projects/new-root',
							bootloader_files: JSON.stringify(['PROJECT.md']),
						},
					}),
				};
			}
			if (url.startsWith('/api/projects/corsia')) {
				return {
					ok: true,
					json: async () => ({
						id: 'corsia',
						name: 'CORSIA',
						description: '',
						status: 'active',
						accent_color: null,
						path: null,
						projects_root: '/workspace/projects',
						bootloader_files: JSON.stringify(['PROJECT.md']),
					}),
				};
			}
			if (url.startsWith('/api/agents')) {
				return {
					ok: true,
					json: async () => ({ agents: [] }),
				};
			}
			if (url.startsWith('/api/fs/browse')) {
				return {
					ok: true,
					json: async () => ({
						path: '/workspace/projects/new-root',
						parent: '/workspace',
						dirs: [],
						files: [],
					}),
				};
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ProjectSettingsView, { target: host });

		await vi.waitFor(() => expect(host.textContent).toContain('Scope and roots'));
		expect(host.textContent).toContain('Project boundary');
		expect(host.textContent).not.toContain('project:corsia');
		expect(host.textContent).toContain('Working root missing');

		host.querySelector<HTMLButtonElement>('button.repair-root')?.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Choose this folder'));
		// The picker uses the server-supplied root and cannot leave it.
		const browsed = fetchMock.mock.calls
			.map((call) => String(call[0]))
			.find((url) => url.startsWith('/api/fs/browse'));
		expect(decodeURIComponent(browsed ?? '')).toContain('path=/workspace/projects');
		host.querySelectorAll<HTMLButtonElement>('button')
			.forEach((button) => {
				if (button.textContent?.includes('Choose this folder')) button.click();
			});

		await vi.waitFor(() => {
			const patch = fetchMock.mock.calls.find(
				(call) =>
					String(call[0]).startsWith('/api/projects/corsia') &&
					(call[1] as RequestInit | undefined)?.method === 'PATCH',
			);
			expect(patch).toBeTruthy();
			expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({
				path: '/workspace/projects/new-root',
			});
			expect(host.textContent).toContain('/workspace/projects/new-root');
		});
	});

	it('keeps project Agent management roster-only in project settings', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/projects/corsia/repos')) {
				return { ok: true, json: async () => ({ repos: [] }) };
			}
			if (url.startsWith('/api/skills')) {
				return { ok: true, json: async () => ({ skills: [], root: '/workspace/projects/corsia', scope_id: 'corsia' }) };
			}
			if (url.startsWith('/api/projects/corsia')) {
				return {
					ok: true,
					json: async () => ({
						id: 'corsia',
						name: 'CORSIA',
						description: '',
						status: 'active',
						accent_color: null,
						path: '/Users/alice/Documents/projects/corsia',
						bootloader_files: JSON.stringify(['PROJECT.md']),
					}),
				};
			}
			if (url.startsWith('/api/agents')) {
				return {
					ok: true,
					json: async () => ({ agents: [] }),
				};
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ProjectSettingsView, { target: host });

		await vi.waitFor(() => expect(host.textContent).toContain('Project Agents'));
		expect(host.querySelector('select[aria-label="Thread to promote"]')).toBeNull();
		expect(host.querySelector('input[aria-label="Responsibility"]')).toBeNull();
	});

	it('shows directory-backed project context without selection controls', async () => {
		const onBrowseContextFiles = vi.fn();
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.startsWith('/api/projects/corsia/repos')) {
				return { ok: true, json: async () => ({ repos: [] }) };
			}
			if (url.startsWith('/api/skills')) {
				return { ok: true, json: async () => ({ skills: [], root: '/workspace/projects/corsia', scope_id: 'corsia' }) };
			}
			if (url.startsWith('/api/projects/corsia')) {
				return {
					ok: true,
					json: async () => ({
						id: 'corsia',
						name: 'CORSIA',
						description: '',
						status: 'active',
						accent_color: null,
						path: '/Users/alice/Documents/projects/corsia',
						bootloader_files: JSON.stringify(['working/STALE.md']),
						context_files: ['context/PROJECT.md'],
					}),
				};
			}
			if (url.startsWith('/api/agents')) {
				return { ok: true, json: async () => ({ agents: [] }) };
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ProjectSettingsView, {
			target: host,
			props: { onBrowseContextFiles },
		});

		await vi.waitFor(() => expect(host.textContent).toContain('Project context'));
		expect(host.textContent).toContain('PROJECT.md');
		expect(host.textContent).not.toContain('STALE.md');
		expect(host.textContent).not.toContain('Add existing');
		expect(host.querySelector('button[title="Remove from project context"]')).toBeNull();
		expect(onBrowseContextFiles).not.toHaveBeenCalled();
		expect(get(contextAttachTarget)).toBeNull();
	});

	it('shows the project Agent roster without a project-level creation form', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/projects/corsia/repos')) {
				return { ok: true, json: async () => ({ repos: [] }) };
			}
			if (url.startsWith('/api/skills')) {
				return { ok: true, json: async () => ({ skills: [], root: '/workspace/projects/corsia', scope_id: 'corsia' }) };
			}
			if (url.startsWith('/api/projects/corsia')) {
				return {
					ok: true,
					json: async () => ({
						id: 'corsia',
						name: 'CORSIA',
						description: '',
						status: 'active',
						accent_color: null,
						path: '/Users/alice/Documents/projects/corsia',
						bootloader_files: JSON.stringify(['PROJECT.md']),
					}),
				};
			}
			if (url.startsWith('/api/agents')) {
				return {
					ok: true,
					json: async () => ({
						agents: [{
							thread_id: 'thread-release',
							display_name: 'Release Coordinator',
							project_id: 'corsia',
							responsibility: 'Coordinates release gates',
							responsibility_paths: ['releases/'],
							responsibility_domains: ['release coordination'],
							active: true,
						}],
					}),
				};
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ProjectSettingsView, { target: host });

		await vi.waitFor(() => expect(host.textContent).toContain('Project Agents'));
		expect(host.textContent).toContain('Release Coordinator');
		expect(host.textContent).toContain('Coordinates release gates');
		expect(host.querySelector('select[aria-label="Thread to promote"]')).toBeNull();
		expect(host.querySelector('input[aria-label="Responsibility"]')).toBeNull();
		expect(host.querySelector('input[aria-label="Responsibility paths"]')).toBeNull();
		expect(host.querySelector('input[aria-label="Responsibility domains"]')).toBeNull();
		expect(fetchMock.mock.calls.some((call) =>
			String(call[0]).startsWith('/api/agents/thread-release') &&
			(call[1] as RequestInit | undefined)?.method === 'PATCH'
		)).toBe(false);
	});

	it('shows project skills with project-scoped browse action', async () => {
		const onBrowseSkills = vi.fn();
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.startsWith('/api/projects/corsia/repos')) {
				return { ok: true, json: async () => ({ repos: [] }) };
			}
			if (url.startsWith('/api/skills')) {
				return {
					ok: true,
					json: async () => ({
						skills: [{
							name: 'deploy',
							description: 'Use when preparing a project deploy.',
							scope: 'project',
							scope_id: 'corsia',
							path: '/workspace/projects/corsia/skills/deploy/SKILL.md',
							body_path: '/workspace/projects/corsia/skills/deploy/SKILL.md',
							metadata: {},
							status: 'included',
							errors: [],
						}],
						root: '/workspace/projects/corsia',
						scope_id: 'corsia',
					}),
				};
			}
			if (url.startsWith('/api/projects/corsia')) {
				return {
					ok: true,
					json: async () => ({
						id: 'corsia',
						name: 'CORSIA',
						description: '',
						status: 'active',
						accent_color: null,
						path: '/Users/alice/Documents/projects/corsia',
						bootloader_files: JSON.stringify(['PROJECT.md']),
					}),
				};
			}
			if (url.startsWith('/api/agents')) {
				return { ok: true, json: async () => ({ agents: [] }) };
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ProjectSettingsView, {
			target: host,
			props: { onBrowseSkills },
		});

		await vi.waitFor(() => expect(host.textContent).toContain('Project skills'));
		expect(host.textContent).toContain('Managing skills for Project: CORSIA');
		expect(host.textContent).toContain('deploy');

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Browse')
			?.click();
		expect(onBrowseSkills).toHaveBeenCalledTimes(1);
	});
});
