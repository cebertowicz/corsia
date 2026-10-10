import { describe, it, expect, vi, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { threads, saveThreadProject } from '$lib/stores/threads';

function seed(projectId: string | null) {
	threads.set([{ id: 's1', project_id: projectId } as never]);
}
function currentPid(): string | null {
	return get(threads).find((s) => s.id === 's1')?.project_id ?? null;
}

describe('saveThreadProject', () => {
	afterEach(() => vi.restoreAllMocks());

	it('keeps the optimistic move when the PATCH succeeds', async () => {
		seed(null);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200 })));
		await saveThreadProject('s1', 'p1', 'tok');
		expect(currentPid()).toBe('p1');
	});

	it('rolls back AND re-throws when the PATCH returns !res.ok', async () => {
		seed(null);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500 })));
		await expect(saveThreadProject('s1', 'p1', 'tok')).rejects.toThrow();
		expect(currentPid()).toBe(null);
	});

	it('rolls back to the PRIOR project AND re-throws on a network error', async () => {
		seed('p0');
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network down'); }));
		await expect(saveThreadProject('s1', null, 'tok')).rejects.toThrow();
		expect(currentPid()).toBe('p0');
	});
});
