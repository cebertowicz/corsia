import { get } from 'svelte/store';
import {
	threads,
	reasoningEffort,
	adoptCreatedThread,
	demoteAgentLocally,
	saveThreadTitle,
	upsertVisibleThread,
	type Thread,
} from './threads';

function makeThread(overrides: Partial<Thread> = {}): Thread {
	return {
		id: 'test-' + Math.random().toString(36).slice(2, 8),
		user_id: 'u1',
		profile: 'personal',
		title: 'Test',
		model: null,
		created_at: new Date().toISOString(),
		updated_at: new Date().toISOString(),
		...overrides,
	};
}

beforeEach(() => {
	threads.set([]);
	reasoningEffort.set(null);
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('adoptCreatedThread reasoning_effort', () => {
	it('preserves reasoning_effort on the adopted session row', () => {
		const s = makeThread({ reasoning_effort: 'high' });
		adoptCreatedThread(s);
		const row = get(threads).find((r) => r.id === s.id);
		expect(row?.reasoning_effort).toBe('high');
	});

	it('session row with null effort survives upsert', () => {
		const s = makeThread({ reasoning_effort: null });
		adoptCreatedThread(s);
		const row = get(threads).find((r) => r.id === s.id);
		expect(row?.reasoning_effort ?? null).toBeNull();
	});

	it('upsertVisibleThread from WS carries reasoning_effort', () => {
		const s = makeThread({ reasoning_effort: 'medium' });
		upsertVisibleThread(s);
		const row = get(threads).find((r) => r.id === s.id);
		expect(row?.reasoning_effort).toBe('medium');
	});

	it('upsert of same session id preserves latest reasoning_effort', () => {
		const s = makeThread({ reasoning_effort: 'low' });
		upsertVisibleThread(s);
		upsertVisibleThread({ ...s, reasoning_effort: 'high', updated_at: new Date(Date.now() + 1000).toISOString() });
		const rows = get(threads).filter((r) => r.id === s.id);
		expect(rows).toHaveLength(1);
		expect(rows[0].reasoning_effort).toBe('high');
	});
});

describe('saveThreadTitle', () => {
	it('updates the shared session row and persists the title', async () => {
		const thread = makeThread({ id: 'agent-1', title: 'Old title' });
		threads.set([thread]);
		const fetchMock = vi.fn().mockResolvedValue({ ok: true });
		vi.stubGlobal('fetch', fetchMock);

		await saveThreadTitle(thread.id, 'New title', 'token with spaces');

		expect(get(threads)[0].title).toBe('New title');
		expect(fetchMock).toHaveBeenCalledWith(
			'/api/threads/agent-1?token=token%20with%20spaces',
			expect.objectContaining({ method: 'PATCH' }),
		);
	});

	it('rolls back the optimistic title when persistence fails', async () => {
		const thread = makeThread({ id: 'agent-1', title: 'Old title' });
		threads.set([thread]);
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: false,
			status: 500,
			json: async () => ({ detail: 'nope' }),
		}));

		await expect(saveThreadTitle(thread.id, 'New title', 'token')).rejects.toThrow('Rename failed (500)');
		expect(get(threads)[0].title).toBe('Old title');
	});
});

describe('demoteAgentLocally', () => {
	it('resets agent_enabled and agent_status but keeps responsibility, paths and domains', () => {
		const thread = makeThread({
			id: 'agent-1',
			agent_enabled: 1,
			agent_status: 'active',
			agent_responsibility: 'Coordinates releases.',
			agent_paths: ['releases/'],
			agent_domains: ['release coordination'],
			agent_notes: 'Ping first.',
		});
		threads.set([thread]);

		demoteAgentLocally('agent-1');

		expect(get(threads)[0]).toMatchObject({
			agent_enabled: 0,
			agent_status: 'inactive',
			agent_responsibility: 'Coordinates releases.',
			agent_paths: ['releases/'],
			agent_domains: ['release coordination'],
			agent_notes: 'Ping first.',
		});
	});
});
