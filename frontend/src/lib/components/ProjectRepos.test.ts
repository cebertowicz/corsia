import { mount, tick, unmount } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ProjectRepos from './ProjectRepos.svelte';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';

type Repo = {
	name: string;
	path: string;
	kind: 'linked' | 'native';
	missing: boolean;
	git: { is_repo: boolean; branch: string | null };
	registered: boolean;
	pinned_branch: string | null;
	protect_base_checkout: boolean;
};

type TestResponse = {
	ok: boolean;
	status: number;
	json: () => Promise<unknown>;
};

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function repo(overrides: Partial<Repo> = {}): Repo {
	return {
		name: 'app',
		path: '/Users/alice/code/app',
		kind: 'linked',
		missing: false,
		git: { is_repo: true, branch: 'main' },
		registered: true,
		pinned_branch: 'main',
		protect_base_checkout: true,
		...overrides,
	};
}

function response(body: unknown, ok = true, status = ok ? 200 : 409): TestResponse {
	return { ok, status, json: async () => body };
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

async function mountLoaded(fetchMock: ReturnType<typeof vi.fn>) {
	vi.stubGlobal('fetch', fetchMock);
	component = mount(ProjectRepos, {
		target: host,
		props: { projectId: 'p1' },
	});
	await vi.waitFor(() => {
		expect(host.querySelector('.hint')?.textContent).not.toBe('Loading…');
	});
}

function button(label: string): HTMLButtonElement {
	const found = host.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
	if (!found) throw new Error(`Missing button: ${label}`);
	return found;
}

function input(label: string): HTMLInputElement {
	const found = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`);
	if (!found) throw new Error(`Missing input: ${label}`);
	return found;
}

beforeEach(() => {
	token.set('test-token');
	host = document.createElement('div');
	document.body.appendChild(host);
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(),
			cancel: vi.fn(),
		})) as unknown as typeof Element.prototype.animate;
	}
	vi.spyOn(toasts, 'show').mockImplementation(() => {});
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
});

describe('ProjectRepos repository policy controls', () => {
	it('renders canonical repo state and hides controls for unregistered and non-Git entries', async () => {
		const fetchMock = vi.fn(async () =>
			response({
				repos: [
					repo(),
					repo({
						name: 'unregistered',
						path: '/Users/alice/code/unregistered',
						registered: false,
						pinned_branch: null,
						protect_base_checkout: false,
					}),
					repo({
						name: 'notes',
						path: '/Users/alice/notes',
						git: { is_repo: false, branch: null },
						pinned_branch: null,
						protect_base_checkout: false,
					}),
				],
			}),
		);
		await mountLoaded(fetchMock);

		expect(host.textContent).toContain('main');
		expect(host.textContent).toContain('unregistered');
		expect(host.textContent).toContain('not a git repo');
		expect(button('Pin app to a branch').getAttribute('aria-pressed')).toBe('true');
		expect(button('Protect app base checkout').getAttribute('aria-pressed')).toBe('true');
		expect(host.querySelector('button[aria-label="Pin unregistered to a branch"]')).toBeNull();
		expect(
			host.querySelector('button[aria-label="Protect unregistered base checkout"]'),
		).toBeNull();
		expect(
			host.querySelector('button[aria-label="Detach unregistered"]'),
		).toBeNull();
		expect(host.querySelector('button[aria-label="Pin notes to a branch"]')).toBeNull();
		expect(host.querySelector('button[aria-label="Protect notes base checkout"]')).toBeNull();
	});

	it('pins the current branch and unpins without changing checkout protection', async () => {
		let current = repo({ pinned_branch: null, protect_base_checkout: true });
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PATCH') {
				const patch = JSON.parse(String(init.body));
				current = { ...current, ...patch };
				return response({ updated: true, repo: current });
			}
			return response({ repos: [current] });
		});
		await mountLoaded(fetchMock);

		button('Pin app to a branch').click();
		await vi.waitFor(() => {
			expect(button('Pin app to a branch').getAttribute('aria-pressed')).toBe('true');
		});

		button('Pin app to a branch').click();
		await vi.waitFor(() => {
			expect(button('Pin app to a branch').getAttribute('aria-pressed')).toBe('false');
		});

		const payloads = fetchMock.mock.calls
			.filter((call) => (call[1] as RequestInit | undefined)?.method === 'PATCH')
			.map((call) => JSON.parse(String((call[1] as RequestInit).body)));
		expect(payloads).toEqual([{ pinned_branch: 'main' }, { pinned_branch: null }]);
		expect(button('Protect app base checkout').getAttribute('aria-pressed')).toBe('true');
		expect(JSON.stringify(payloads)).not.toContain('branch_pinned');
	});

	it('PATCHes checkout protection independently and keeps server state on failure', async () => {
		let current = repo();
		let attempts = 0;
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PATCH') {
				attempts += 1;
				if (attempts === 2) {
					return response({ detail: 'Checkout protection is required' }, false);
				}
				const patch = JSON.parse(String(init.body));
				current = { ...current, ...patch };
				return response({ updated: true, repo: current });
			}
			return response({ repos: [current] });
		});
		await mountLoaded(fetchMock);

		button('Protect app base checkout').click();
		await vi.waitFor(() => {
			expect(button('Protect app base checkout').getAttribute('aria-pressed')).toBe('false');
		});
		expect(button('Pin app to a branch').getAttribute('aria-pressed')).toBe('true');

		button('Protect app base checkout').click();
		await vi.waitFor(() => {
			expect(toasts.show).toHaveBeenCalledWith('Checkout protection is required', 'error');
		});
		expect(button('Protect app base checkout').getAttribute('aria-pressed')).toBe('false');

		const payloads = fetchMock.mock.calls
			.filter((call) => (call[1] as RequestInit | undefined)?.method === 'PATCH')
			.map((call) => JSON.parse(String((call[1] as RequestInit).body)));
		expect(payloads).toEqual([
			{ protect_base_checkout: false },
			{ protect_base_checkout: true },
		]);
	});

	it('loads branches lazily and PATCHes a selected branch', async () => {
		let current = repo();
		const fetchMock = vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
			if (String(request).includes('/branches')) {
				return response({
					current_branch: 'main',
					branches: [
						{ name: 'main', source: 'local', remote: null },
						{ name: 'release', source: 'remote', remote: 'origin' },
					],
				});
			}
			if (init?.method === 'PATCH') {
				const patch = JSON.parse(String(init.body));
				current = {
					...current,
					...patch,
					git: { ...current.git, branch: patch.pinned_branch },
				};
				return response({ updated: true, repo: current });
			}
			return response({ repos: [current] });
		});
		await mountLoaded(fetchMock);

		expect(fetchMock.mock.calls.some((call) => String(call[0]).includes('/branches'))).toBe(false);
		expect(host.querySelector('select[aria-label="Pinned branch for app"]')).toBeNull();

		button('Choose branch for app').click();
		const select = await vi.waitFor(() => {
			const found = host.querySelector<HTMLSelectElement>(
				'select[aria-label="Pinned branch for app"]',
			);
			expect(Array.from(found?.options ?? []).map((option) => option.value)).toEqual([
				'main',
				'release',
			]);
			return found;
		});
		if (!select) throw new Error('Missing branch selector');
		expect(select.options[1]?.textContent).toBe('release (origin)');

		select.value = 'release';
		select.dispatchEvent(new Event('change', { bubbles: true }));

		await vi.waitFor(() => {
			expect(select.value).toBe('release');
			const patch = fetchMock.mock.calls.find(
				(call) => (call[1] as RequestInit | undefined)?.method === 'PATCH',
			);
			expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({
				pinned_branch: 'release',
			});
		});
	});

	it('rolls a failed branch change back to the persisted selection', async () => {
		const current = repo();
		const fetchMock = vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
			if (String(request).includes('/branches')) {
				return response({
					current_branch: 'main',
					branches: [
						{ name: 'main', source: 'local', remote: null },
						{ name: 'release', source: 'local', remote: null },
					],
				});
			}
			if (init?.method === 'PATCH') {
				return response({ detail: 'Checkout has local changes' }, false);
			}
			return response({ repos: [current] });
		});
		await mountLoaded(fetchMock);

		button('Choose branch for app').click();
		const select = await vi.waitFor(() => {
			const found = host.querySelector<HTMLSelectElement>(
				'select[aria-label="Pinned branch for app"]',
			);
			expect(found?.value).toBe('main');
			return found;
		});
		if (!select) throw new Error('Missing branch selector');

		select.value = 'release';
		select.dispatchEvent(new Event('change', { bubbles: true }));

		await vi.waitFor(() => {
			expect(select.value).toBe('main');
			expect(toasts.show).toHaveBeenCalledWith('Checkout has local changes', 'error');
		});
	});

	it('omits canonical add controls for Git-only defaults', async () => {
		const fetchMock = vi.fn(async (_request: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') return response({ added: true, repo: repo() });
			return response({ repos: [] });
		});
		await mountLoaded(fetchMock);

		host.querySelector<HTMLButtonElement>('.add')?.click();
		await vi.waitFor(() => expect(host.querySelector('[role="dialog"]')).not.toBeNull());
		const pathField = input('Repository path');
		pathField.value = '/Users/alice/code/app';
		pathField.dispatchEvent(new Event('input', { bubbles: true }));
		const submit = host.querySelector<HTMLButtonElement>('button.primary');
		await vi.waitFor(() => expect(submit?.disabled).toBe(false));
		submit?.click();

		await vi.waitFor(() => {
			expect(
				fetchMock.mock.calls.some(
					(call) => (call[1] as RequestInit | undefined)?.method === 'POST',
				),
			).toBe(true);
		});
		const post = fetchMock.mock.calls.find(
			(call) => (call[1] as RequestInit | undefined)?.method === 'POST',
		);
		expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({
			mode: 'link',
			path: '/Users/alice/code/app',
			name: null,
		});
	});

	it('sends explicit add opt-outs without legacy compatibility fields', async () => {
		const fetchMock = vi.fn(async (_request: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'POST') {
				return response({
					added: true,
					repo: repo({ pinned_branch: null, protect_base_checkout: false }),
				});
			}
			return response({ repos: [] });
		});
		await mountLoaded(fetchMock);

		host.querySelector<HTMLButtonElement>('.add')?.click();
		await vi.waitFor(() => expect(host.querySelector('[role="dialog"]')).not.toBeNull());
		button('Pin current branch for added repository').click();
		button('Protect base checkout for added repository').click();
		const pathField = input('Repository path');
		pathField.value = '/Users/alice/notes';
		pathField.dispatchEvent(new Event('input', { bubbles: true }));
		const submit = host.querySelector<HTMLButtonElement>('button.primary');
		await vi.waitFor(() => expect(submit?.disabled).toBe(false));
		submit?.click();

		await vi.waitFor(() => {
			expect(
				fetchMock.mock.calls.some(
					(call) => (call[1] as RequestInit | undefined)?.method === 'POST',
				),
			).toBe(true);
		});
		const post = fetchMock.mock.calls.find(
			(call) => (call[1] as RequestInit | undefined)?.method === 'POST',
		);
		const body = JSON.parse(String((post?.[1] as RequestInit).body));
		expect(body).toEqual({
			mode: 'link',
			path: '/Users/alice/notes',
			name: null,
			pinned_branch: null,
			protect_base_checkout: false,
		});
		expect(body).not.toHaveProperty('branch_pinned');
	});

	it('ignores a stale repository response after the selected project changes', async () => {
		const first = deferred<TestResponse>();
		const second = deferred<TestResponse>();
		const fetchMock = vi.fn(async (request: RequestInfo | URL) => {
			if (String(request).includes('/projects/p1/')) return first.promise;
			if (String(request).includes('/projects/p2/')) return second.promise;
			throw new Error(`Unexpected request: ${String(request)}`);
		});
		vi.stubGlobal('fetch', fetchMock);
		const projectId = writable('p1');
		const reactiveProjectId = fromStore(projectId);
		const props = {
			get projectId() {
				return reactiveProjectId.current;
			},
		};
		component = mount(ProjectRepos, { target: host, props });
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

		projectId.set('p2');
		await vi.waitFor(() => {
			expect(
				fetchMock.mock.calls.some((call) => String(call[0]).includes('/projects/p2/')),
			).toBe(true);
		});

		const staleJson = vi.fn(async () => ({
			repos: [repo({ path: '/old/p1/app' })],
		}));
		first.resolve({ ok: true, status: 200, json: staleJson });
		await vi.waitFor(() => expect(staleJson).toHaveBeenCalledOnce());
		await tick();
		expect(host.textContent).not.toContain('/old/p1/app');
		expect(host.querySelector('.hint')?.textContent).toBe('Loading…');

		second.resolve(response({ repos: [repo({ path: '/current/p2/app' })] }));
		await vi.waitFor(() => expect(host.textContent).toContain('/current/p2/app'));
		expect(host.textContent).not.toContain('/old/p1/app');
	});

	it('uses a compact summary/settings structure that can stack on mobile', async () => {
		const fetchMock = vi.fn(async () => response({ repos: [repo()] }));
		await mountLoaded(fetchMock);

		const item = host.querySelector('.repo-item');
		expect(item?.querySelector(':scope > .repo-summary')).not.toBeNull();
		expect(item?.querySelector(':scope > .repo-settings')).not.toBeNull();
		expect(item?.querySelectorAll('.repo-setting')).toHaveLength(2);
	});
});
