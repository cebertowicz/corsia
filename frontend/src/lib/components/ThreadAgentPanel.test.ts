import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import ThreadAgentPanel from './ThreadAgentPanel.svelte';
import { token } from '$lib/stores/auth';
import { threads, type Thread } from '$lib/stores/threads';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const agentThread: Thread = {
	id: 'thread-release',
	user_id: 'steve',
	profile: 'default',
	title: 'Release Coordinator',
	model: null,
	project_id: null,
	created_at: '2026-07-01T00:00:00Z',
	updated_at: '2026-07-01T00:00:00Z',
};

beforeEach(() => {
	token.set('test-token');
	threads.set([agentThread]);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
	threads.set([]);
});

describe('ThreadAgentPanel', () => {
	it('promotes a workspace thread into an Agent with explicit responsibility scope', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/agents/thread-release') && init?.method === 'PATCH') {
				return { ok: true, json: async () => ({ ok: true }) };
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ThreadAgentPanel, { target: host, props: { thread: agentThread } });
		flushSync();

		expect(host.textContent).toContain('Not an agent');
		expect(host.querySelector('select')).toBeNull();
		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Make it an agent')
			?.click();
		flushSync();

		const description = host.querySelector<HTMLTextAreaElement>('textarea[aria-label="Responsibility"]');
		const pathInput = host.querySelector<HTMLInputElement>('input[aria-label="Responsibility path"]');
		const topicInput = host.querySelector<HTMLInputElement>('input[aria-label="Responsibility topic"]');
		expect(description).toBeTruthy();
		expect(pathInput).toBeTruthy();
		expect(topicInput).toBeTruthy();

		description!.value = 'Coordinates release gates';
		description!.dispatchEvent(new Event('input', { bubbles: true }));
		pathInput!.value = 'releases/';
		pathInput!.dispatchEvent(new Event('input', { bubbles: true }));
		pathInput!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		topicInput!.value = 'release coordination';
		topicInput!.dispatchEvent(new Event('input', { bubbles: true }));
		topicInput!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Save')
			?.click();

		await vi.waitFor(() => {
			const patch = fetchMock.mock.calls.find(
				(call) => String(call[0]).startsWith('/api/agents/thread-release') &&
					(call[1] as RequestInit | undefined)?.method === 'PATCH',
			);
			expect(patch).toBeTruthy();
			expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({
				responsibility: 'Coordinates release gates',
				responsibility_paths: ['releases/'],
				responsibility_domains: ['release coordination'],
				active: true,
			});
			expect(get(threads)[0]).toMatchObject({
				id: 'thread-release',
				agent_enabled: 1,
				agent_responsibility: 'Coordinates release gates',
				agent_paths: ['releases/'],
				agent_domains: ['release coordination'],
			});
		});
	});

	it('demoting an Agent keeps its responsibility, paths and topics for re-promoting', async () => {
		threads.set([{
			...agentThread,
			agent_enabled: 1,
			agent_responsibility: 'Coordinates release gates',
			agent_paths: ['releases/'],
			agent_domains: ['release coordination'],
			agent_status: 'active',
		}]);

		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.startsWith('/api/agents/thread-release') && init?.method === 'DELETE') {
				return { ok: true, json: async () => ({ ok: true }) };
			}
			throw new Error(`Unexpected fetch: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(ThreadAgentPanel, { target: host, props: { thread: get(threads)[0] } });
		flushSync();
		expect(host.textContent).toContain('Coordinates release gates');

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Remove')
			?.click();

		await vi.waitFor(() => {
			expect(get(threads)[0]).toMatchObject({
				agent_enabled: 0,
				agent_status: 'inactive',
				agent_responsibility: 'Coordinates release gates',
				agent_paths: ['releases/'],
				agent_domains: ['release coordination'],
			});
		});

		unmount(component);
		component = mount(ThreadAgentPanel, { target: host, props: { thread: get(threads)[0] } });
		flushSync();
		expect(host.textContent).toContain('Not an agent');

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Make it an agent')
			?.click();
		flushSync();

		const description = host.querySelector<HTMLTextAreaElement>('textarea[aria-label="Responsibility"]');
		expect(description?.value).toBe('Coordinates release gates');
		const chipText = Array.from(host.querySelectorAll('.token-chip')).map((el) => el.textContent).join(' ');
		expect(chipText).toContain('releases/');
		expect(chipText).toContain('release coordination');
	});
});
