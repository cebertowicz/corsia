import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { _resetProjectsForTests, _setProjectsForTests, enterThreadScope } from '$lib/stores/projects';
import { uiScope } from '$lib/stores/uiScope';
import type { Thread } from '$lib/stores/threads';

function thread(projectId: string | null | undefined): Thread {
	return {
		id: 's1',
		user_id: 'u1',
		profile: 'personal',
		title: 'T',
		model: null,
		project_id: projectId,
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
	};
}

describe('enterThreadScope', () => {
	beforeEach(() => {
		token.set('tok');
		_resetProjectsForTests();
		uiScope.set(null);
	});
	afterEach(() => {
		vi.restoreAllMocks();
		token.set(null);
		_resetProjectsForTests();
		uiScope.set(null);
	});

	it('enters the project scope for a project-scoped session when the project is already known', async () => {
		_setProjectsForTests([{ id: 'p1', name: 'Proj', status: 'active', accent_color: '#16a34a' }]);
		await enterThreadScope(thread('p1'));
		expect(get(uiScope)).toEqual({ id: 'p1', name: 'Proj', color: '#16a34a' });
	});

	it('loads projects on demand when the project row is missing (cold start)', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({
			ok: true,
			json: async () => ({ projects: [{ id: 'p2', name: 'Late', status: 'active', accent_color: null }] }),
		})));
		await enterThreadScope(thread('p2'));
		expect(get(uiScope)?.id).toBe('p2');
		expect(get(uiScope)?.name).toBe('Late');
	});

	it('exits to the workspace scope for an unassigned session', async () => {
		uiScope.set({ id: 'p1', name: 'Proj', color: '#16a34a' });
		await enterThreadScope(thread(null));
		expect(get(uiScope)).toBe(null);
	});

	it('leaves the scope untouched for an unknown session row (hidden subagent)', async () => {
		uiScope.set({ id: 'p1', name: 'Proj', color: '#16a34a' });
		await enterThreadScope(undefined);
		expect(get(uiScope)).toEqual({ id: 'p1', name: 'Proj', color: '#16a34a' });
	});
});