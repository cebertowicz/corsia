import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import {
	_resetProjectsForTests,
	loadProjects,
	projectLoadError,
	projectLoadStatus,
	projects,
} from '$lib/stores/projects';
import { emit } from '$lib/ws/events';

function response(projectRows: unknown[]) {
	return { ok: true, json: async () => ({ projects: projectRows }) };
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((complete) => { resolve = complete; });
	return { promise, resolve };
}

describe('projects_changed nudge', () => {
	beforeEach(() => {
		_resetProjectsForTests();
		token.set('catalog-token');
		_resetProjectsForTests();
	});

	afterEach(() => {
		_resetProjectsForTests();
		token.set(null);
		vi.unstubAllGlobals();
	});

	it('re-reads a loaded catalog so a project created elsewhere appears without a reload', async () => {
		const rows: unknown[] = [{ id: 'p1', name: 'One', status: 'active', accent_color: null }];
		const fetchMock = vi.fn(async () => response(rows));
		vi.stubGlobal('fetch', fetchMock);
		await loadProjects();
		rows.push({ id: 'p2', name: 'Two', status: 'active', accent_color: null });

		emit('projects_changed', { type: 'projects_changed' });
		await vi.waitFor(() => expect(get(projects).map((p) => p.id)).toEqual(['p1', 'p2']));

		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('does not let a read begun before the nudge answer for it', async () => {
		const first = deferred<ReturnType<typeof response>>();
		const fetchMock = vi.fn()
			.mockReturnValueOnce(first.promise)
			.mockResolvedValueOnce(response([
				{ id: 'p1', name: 'One', status: 'active', accent_color: null },
				{ id: 'p2', name: 'Two', status: 'active', accent_color: null },
			]));
		vi.stubGlobal('fetch', fetchMock);
		const initial = loadProjects();

		emit('projects_changed', { type: 'projects_changed' });
		first.resolve(response([{ id: 'p1', name: 'One', status: 'active', accent_color: null }]));
		await initial;

		await vi.waitFor(() => expect(get(projects).map((p) => p.id)).toEqual(['p1', 'p2']));
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});
});

describe('projects catalog load ownership', () => {
	beforeEach(() => {
		_resetProjectsForTests();
		token.set('catalog-token');
		_resetProjectsForTests();
	});

	afterEach(() => {
		_resetProjectsForTests();
		token.set(null);
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	it('loads a nonempty catalog once and records the terminal loaded state', async () => {
		const fetchMock = vi.fn(async () => response([
			{ id: 'p1', name: 'CORSIA', status: 'active', accent_color: null },
		]));
		vi.stubGlobal('fetch', fetchMock);

		await loadProjects();

		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(get(projects)).toEqual([{ id: 'p1', name: 'CORSIA', status: 'active', accent_color: null }]);
		expect(get(projectLoadStatus)).toBe('loaded');
		expect(get(projectLoadError)).toBeNull();
	});

	it('treats an empty catalog as loaded and does not re-request it', async () => {
		const fetchMock = vi.fn(async () => response([]));
		vi.stubGlobal('fetch', fetchMock);

		await Promise.all([loadProjects(), loadProjects()]);
		await loadProjects();

		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(get(projects)).toEqual([]);
		expect(get(projectLoadStatus)).toBe('loaded');
	});

	it('coalesces concurrent callers onto one request', async () => {
		const pending = deferred<ReturnType<typeof response>>();
		const fetchMock = vi.fn(() => pending.promise);
		vi.stubGlobal('fetch', fetchMock);

		const first = loadProjects();
		const second = loadProjects();
		expect(first).toBe(second);
		expect(fetchMock).toHaveBeenCalledTimes(1);

		pending.resolve(response([]));
		await first;
		expect(get(projectLoadStatus)).toBe('loaded');
	});

	it('allows an explicit refresh after a successful load', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(response([]))
			.mockResolvedValueOnce(response([{ id: 'p2', name: 'Refreshed', status: 'active' }]));
		vi.stubGlobal('fetch', fetchMock);

		await loadProjects();
		await loadProjects({ force: true });

		expect(fetchMock).toHaveBeenCalledTimes(2);
		expect(get(projects)[0]?.name).toBe('Refreshed');
	});

	it('exposes a retryable error without a retry loop', async () => {
		const fetchMock = vi
			.fn()
			.mockRejectedValueOnce(new Error('network down'))
			.mockResolvedValueOnce(response([]));
		vi.stubGlobal('fetch', fetchMock);

		await loadProjects();
		expect(fetchMock).toHaveBeenCalledTimes(1);
		expect(get(projectLoadStatus)).toBe('error');
		expect(get(projectLoadError)).toBe('Could not load projects');

		await loadProjects();
		expect(fetchMock).toHaveBeenCalledTimes(2);
		expect(get(projectLoadStatus)).toBe('loaded');
		expect(get(projectLoadError)).toBeNull();
	});

	it('invalidates the catalog across token epochs and discards the old response', async () => {
		const oldRequest = deferred<ReturnType<typeof response>>();
		const fetchMock = vi
			.fn()
			.mockImplementationOnce(() => oldRequest.promise)
			.mockResolvedValueOnce(response([{ id: 'new', name: 'New identity', status: 'active' }]));
		vi.stubGlobal('fetch', fetchMock);

		const staleLoad = loadProjects();
		token.set('new-token');
		await loadProjects();
		oldRequest.resolve(response([{ id: 'old', name: 'Old identity', status: 'active' }]));
		await staleLoad;

		expect(get(projects)).toEqual([{ id: 'new', name: 'New identity', status: 'active', accent_color: null }]);
		expect(get(projectLoadStatus)).toBe('loaded');
	});

	it('reset discards a late response and restores every visible owner field', async () => {
		const pending = deferred<ReturnType<typeof response>>();
		vi.stubGlobal('fetch', vi.fn(() => pending.promise));

		const staleLoad = loadProjects();
		_resetProjectsForTests();
		pending.resolve(response([{ id: 'late', name: 'Must not appear', status: 'active' }]));
		await staleLoad;

		expect(get(projects)).toEqual([]);
		expect(get(projectLoadStatus)).toBe('idle');
		expect(get(projectLoadError)).toBeNull();
	});
});
