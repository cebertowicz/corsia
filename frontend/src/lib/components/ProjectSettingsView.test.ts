import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import ProjectSettingsView from './ProjectSettingsView.svelte';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { SERVER_UNREACHABLE } from '$lib/plainError';
import { uiScope } from '$lib/stores/uiScope';
import { openProjectSettings, resetSettingsNavigation, settingsNavigation } from '$lib/stores/settingsNavigation';

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
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	uiScope.set(null);
	token.set(null);
	resetSettingsNavigation();
});

type Detail = Record<string, unknown>;

function project(overrides: Detail = {}): Detail {
	return {
		id: 'corsia',
		name: 'CORSIA',
		description: '',
		status: 'active',
		accent_color: null,
		path: '/Users/alice/Documents/projects/corsia',
		projects_root: '/workspace/projects',
		...overrides,
	};
}

function stubFetch(detail: Detail, agents: unknown[] = [], extra?: (url: string, init?: RequestInit) => unknown) {
	const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		const handled = extra?.(url, init);
		if (handled) return handled;
		if (url.startsWith('/api/projects?')) throw new Error('The project page must not infer its root from sibling projects');
		const base = `/api/projects/${detail.id}`;
		if (url.startsWith(`${base}/repos`)) return { ok: true, json: async () => ({ repos: [] }) };
		if (url.startsWith('/api/provider-policy')) return { ok: true, json: async () => ({ scope: 'project', providers: [] }) };
		if (url.startsWith(base)) return { ok: true, json: async () => detail };
		if (url.startsWith('/api/agents')) return { ok: true, json: async () => ({ agents }) };
		return { ok: true, json: async () => ({}) };
	});
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
}

const button = (label: string) => Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
	.find((candidate) => candidate.textContent?.trim().startsWith(label));

describe('ProjectSettingsView', () => {
	it('names the project and repairs a missing working folder inside the server root', async () => {
		const fetchMock = stubFetch(project({ path: null }), [], (url, init) => {
			if (url.startsWith('/api/projects/corsia') && init?.method === 'PATCH') {
				return { ok: true, json: async () => ({ ok: true, project: project({ path: '/workspace/projects/new-root' }) }) };
			}
			if (url.startsWith('/api/fs/browse')) {
				return { ok: true, json: async () => ({ path: '/workspace/projects/new-root', parent: '/workspace', dirs: [], files: [] }) };
			}
			return null;
		});

		component = mount(ProjectSettingsView, { target: host, props: { projectId: 'corsia' } });

		await vi.waitFor(() => expect(host.querySelector('.page-title')?.textContent).toBe('CORSIA'));
		const heading = host.querySelector('h2.project-title');
		expect(heading?.textContent).toBe('CORSIA');
		expect(heading?.classList.contains('page-title')).toBe(true);
		expect(heading?.parentElement?.firstElementChild).toBe(heading);
		expect(host.textContent).toContain('Working folder missing');

		button('Choose folder')!.click();
		await vi.waitFor(() => expect(Array.from(document.body.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim().startsWith('Choose this folder'))).toBeTruthy());
		const browsed = fetchMock.mock.calls.map((call) => String(call[0])).find((url) => url.startsWith('/api/fs/browse'));
		expect(decodeURIComponent(browsed ?? '')).toContain('path=/workspace/projects');
		Array.from(document.body.querySelectorAll<HTMLButtonElement>('button')).find((candidate) => candidate.textContent?.trim().startsWith('Choose this folder'))!.click();

		await vi.waitFor(() => {
			const patch = fetchMock.mock.calls.find(
				(call) => String(call[0]).startsWith('/api/projects/corsia') && (call[1] as RequestInit | undefined)?.method === 'PATCH',
			);
			expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({ path: '/workspace/projects/new-root' });
			expect(host.textContent).toContain('new-root');
		});
	});

	it('lists project Agents with removal only on the row menu, no creation form', async () => {
		stubFetch(project(), [{
			thread_id: 'thread-release',
			display_name: 'Release Coordinator',
			project_id: 'corsia',
			responsibility: 'Coordinates release gates',
			responsibility_paths: ['releases/'],
			responsibility_domains: ['release coordination'],
			active: true,
		}]);

		component = mount(ProjectSettingsView, { target: host, props: { projectId: 'corsia' } });

		await vi.waitFor(() => expect(host.textContent).toContain('Release Coordinator'));
		expect(host.textContent).toContain('Coordinates release gates');
		expect(host.querySelector('select[aria-label="Thread to promote"]')).toBeNull();
		expect(host.querySelector('input[aria-label="Responsibility"]')).toBeNull();
		expect(button('Remove')).toBeUndefined();
	});

	it('offers Retry when the agent list fails to load, and reloads it', async () => {
		let agentCalls = 0;
		stubFetch(project(), [], (url) => {
			if (!url.startsWith('/api/agents')) return null;
			agentCalls += 1;
			if (agentCalls === 1) return { ok: false, status: 500, json: async () => ({}) };
			return { ok: true, json: async () => ({ agents: [{ thread_id: 't1', display_name: 'Release Coordinator', project_id: 'corsia', active: true }] }) };
		});
		vi.spyOn(console, 'error').mockImplementation(() => {});

		component = mount(ProjectSettingsView, { target: host, props: { projectId: 'corsia' } });

		await vi.waitFor(() => expect(host.textContent).toContain('Couldn’t load this project’s agents.'));
		const retry = host.querySelector<HTMLButtonElement>('.note.failed button');
		expect(retry?.textContent?.trim()).toBe('Retry');
		retry!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Release Coordinator'));
		expect(host.textContent).not.toContain('Couldn’t load this project’s agents.');
		expect(agentCalls).toBe(2);
	});

	it.each([
		['the server refuses', () => ({ ok: false, status: 500, json: async () => ({}) }), 'Couldn’t remove that. Try again.'],
		['the server is unreachable', () => { throw new TypeError('Failed to fetch'); }, SERVER_UNREACHABLE],
	])('keeps the agent list and says so in plain words when %s on remove', async (_case, reply, message) => {
		stubFetch(project(), [{ thread_id: 't1', display_name: 'Release Coordinator', project_id: 'corsia', active: true }], (url, init) => {
			if (url.startsWith('/api/agents/t1') && init?.method === 'DELETE') return reply();
			return null;
		});
		const show = vi.spyOn(toasts, 'show');

		component = mount(ProjectSettingsView, { target: host, props: { projectId: 'corsia' } });
		await vi.waitFor(() => expect(host.textContent).toContain('Release Coordinator'));
		host.querySelector('[data-settings-row="Release Coordinator"]')!.dispatchEvent(
			new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 24, clientY: 24 }),
		);
		flushSync();
		const remove = await vi.waitFor(() => {
			const item = Array.from(document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')).find((el) => el.textContent?.includes('Remove'));
			expect(item).toBeTruthy();
			return item!;
		});
		remove.click();

		await vi.waitFor(() => expect(show).toHaveBeenCalledWith(message, 'error'));
		expect(host.textContent).toContain('Release Coordinator');
	});

	it('opens Skills and Context at this project\'s level, remembering the page to come back to', async () => {
		stubFetch(project());
		component = mount(ProjectSettingsView, { target: host, props: { projectId: 'corsia' } });
		await vi.waitFor(() => expect(host.textContent).toContain('What this project can use'));

		button('Context')!.click();
		expect(get(settingsNavigation).section).toBe('context');
		expect(get(settingsNavigation).anchor).toBe('context-corsia');
		expect(get(settingsNavigation).fromProject).toEqual({ id: 'corsia', name: 'CORSIA' });
		expect(get(settingsNavigation).project).toBeNull();
	});

	it('scrolls to the rollover block that renders only after the project loads', async () => {
		const scroll = vi.fn();
		Element.prototype.scrollIntoView = scroll;
		let release!: () => void;
		const gate = new Promise<void>((resolve) => { release = resolve; });
		stubFetch(project(), [], (url) => {
			if (!url.startsWith('/api/projects/corsia?')) return undefined;
			return gate.then(() => ({ ok: true, json: async () => project() }));
		});
		openProjectSettings('corsia', 'project-provider-policy');
		component = mount(ProjectSettingsView, { target: host, props: { projectId: 'corsia' } });
		flushSync();
		expect(host.querySelector('[data-settings-anchor="project-provider-policy"]')).toBeNull();
		expect(get(settingsNavigation).anchor).toBe('project-provider-policy');

		release();
		await vi.waitFor(() => expect(host.querySelector('[data-settings-anchor="project-provider-policy"]')).not.toBeNull());
		expect(scroll).toHaveBeenCalledTimes(1);
		expect(scroll.mock.contexts[0]).toBe(host.querySelector('[data-settings-anchor="project-provider-policy"]'));
		expect(get(settingsNavigation).anchor).toBeNull();
	});

	it('repaints the shell accent only for the project the person is in', async () => {
		stubFetch(project({ id: 'other', name: 'Other' }));
		uiScope.set({ id: 'corsia', name: 'CORSIA', color: '#16a34a' });
		component = mount(ProjectSettingsView, { target: host, props: { projectId: 'other' } });
		await vi.waitFor(() => expect(host.querySelector('.page-title')?.textContent).toBe('Other'));
		flushSync();
		unmount(component);
		component = null;
		expect(get(uiScope)).toEqual({ id: 'corsia', name: 'CORSIA', color: '#16a34a' });
	});
});
