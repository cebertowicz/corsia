import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	_resetFirstRunLandingForTests,
	conductorScopeParam,
	fetchConductor,
	fetchCorsiaAgent,
	fetchScopeTarget,
	firstRunLanding,
	landOnFirstRun,
	setupIncomplete,
	corsiaAgentId,
} from '../conductor';
import type { UiScope } from '$lib/stores/uiScope';
import { get } from 'svelte/store';
import {
	_resetThreadModelTruthForTests,
	acceptThreadModelTruth,
	activeModelId,
} from '$lib/stores/models';
import { activeThreadId } from '$lib/stores/threads';

const originalFetch = globalThis.fetch;

afterEach(() => {
	vi.restoreAllMocks();
	globalThis.fetch = originalFetch;
	activeThreadId.set(null);
	activeModelId.set(null);
	corsiaAgentId.set(null);
	_resetThreadModelTruthForTests();
});

function mockConductorTarget(model: string, updatedAt: string | undefined) {
	globalThis.fetch = vi.fn(async () => ({
		ok: true,
		json: async () => ({
			scope: 'workspace',
			thread_id: 'cond-1',
			target: { thread_id: 'cond-1', model, updated_at: updatedAt },
		}),
	})) as unknown as typeof fetch;
}

describe('conductorScopeParam', () => {
	it("null scope (Workspace) → 'workspace'", () => {
		expect(conductorScopeParam(null)).toBe('workspace');
	});

	it('a project scope → its id', () => {
		const scope: UiScope = { id: 'proj-123', name: 'CORSIA', color: '#7c5cff' };
		expect(conductorScopeParam(scope)).toBe('proj-123');
	});
});

describe('fetchConductor', () => {
	it('reads a project scope from the conductor route, not the CORSIA agent route', async () => {
		globalThis.fetch = vi.fn(async () => ({
			ok: true,
			json: async () => ({ scope: 'proj-123', thread_id: null, target: null }),
		})) as unknown as typeof fetch;

		await fetchConductor('tok', { id: 'proj-123', name: 'CORSIA', color: '#7c5cff' });

		expect(vi.mocked(globalThis.fetch).mock.calls[0][0]).toBe('/api/conductor?token=tok&scope=proj-123');
	});

	it('preserves the backend target model for hidden conductor hydration', async () => {
		globalThis.fetch = vi.fn(async () => ({
			ok: true,
			json: async () => ({
				scope: 'proj-123',
				thread_id: 'cond-1',
				target: {
					thread_id: 'cond-1',
					title: 'Project Conductor',
					model: 'codex/gpt-5-codex',
					reasoning_effort: 'max',
					driver: 'codex',
				},
			}),
		})) as unknown as typeof fetch;

		const scope: UiScope = { id: 'proj-123', name: 'CORSIA', color: '#7c5cff' };
		const lookup = await fetchConductor('tok', scope);

		expect(lookup).toEqual({
			kind: 'target',
			target: {
				thread_id: 'cond-1',
				title: 'Project Conductor',
				model: 'codex/gpt-5-codex',
				reasoning_effort: 'max',
				driver: 'codex',
			},
			firstRunPending: false,
		});
		// A project conductor is never the CORSIA agent.
		expect(get(corsiaAgentId)).toBeNull();
	});

	it('reads first_run_pending and records the CORSIA agent', async () => {
		globalThis.fetch = vi.fn(async () => ({
			ok: true,
			json: async () => ({
				scope: 'workspace',
				thread_id: 'cond-w',
				first_run_pending: true,
				target: { thread_id: 'cond-w', title: 'CORSIA' },
			}),
		})) as unknown as typeof fetch;

		const lookup = await fetchCorsiaAgent('tok');

		expect(lookup).toMatchObject({ kind: 'target', firstRunPending: true });
		expect(vi.mocked(globalThis.fetch).mock.calls[0][0]).toMatch(/^\/api\/corsia-agent\?token=tok$/);
		expect(get(corsiaAgentId)).toBe('cond-w');
	});

	it('accepts the conductor model as thread truth, so the open conductor rebinds the picker', async () => {
		activeThreadId.set('cond-1');
		activeModelId.set('claude-code/opus');
		mockConductorTarget('codex/gpt-5-codex', '2026-06-18T00:00:00');

		await fetchCorsiaAgent('tok');

		expect(get(activeModelId)).toBe('codex/gpt-5-codex');
	});

	it('orders the conductor read by server time, so a late read never regresses a newer switch', async () => {
		activeThreadId.set('cond-1');
		acceptThreadModelTruth('cond-1', 'omlx/newer', '2026-06-19T00:00:00');
		mockConductorTarget('codex/gpt-5-codex', '2026-06-18T00:00:00');

		await fetchCorsiaAgent('tok');

		expect(get(activeModelId)).toBe('omlx/newer');
	});

	it('leaves the picker alone when the target carries no server time to order by', async () => {
		activeThreadId.set('cond-1');
		activeModelId.set('claude-code/opus');
		mockConductorTarget('codex/gpt-5-codex', undefined);

		await fetchCorsiaAgent('tok');

		expect(get(activeModelId)).toBe('claude-code/opus');
	});

	it('reports absent when the API answers with no conductor', async () => {
		globalThis.fetch = vi.fn(async () => ({
			ok: true,
			json: async () => ({ scope: 'workspace', thread_id: null, target: null }),
		})) as unknown as typeof fetch;

		await expect(fetchCorsiaAgent('tok')).resolves.toEqual({ kind: 'absent' });
	});

	it('reports unknown on a failed transport, never absent', async () => {
		globalThis.fetch = vi.fn(async () => {
			throw new TypeError('network down');
		}) as unknown as typeof fetch;

		await expect(fetchCorsiaAgent('tok')).resolves.toEqual({ kind: 'unknown' });
	});

	it('reports unknown on a non-OK response, never absent', async () => {
		globalThis.fetch = vi.fn(async () => ({ ok: false, status: 500 })) as unknown as typeof fetch;

		await expect(fetchCorsiaAgent('tok')).resolves.toEqual({ kind: 'unknown' });
	});

	it('reports unknown without a token and never calls the API', async () => {
		const fetchMock = vi.fn() as unknown as typeof fetch;
		globalThis.fetch = fetchMock;

		await expect(fetchCorsiaAgent(null)).resolves.toEqual({ kind: 'unknown' });
		expect(fetchMock).not.toHaveBeenCalled();
	});
});

describe('fetchScopeTarget', () => {
	it('reads the CORSIA agent for the workspace and the conductor for a project', async () => {
		globalThis.fetch = vi.fn(async () => ({
			ok: true,
			json: async () => ({ thread_id: null, target: null }),
		})) as unknown as typeof fetch;

		await fetchScopeTarget('tok', null);
		await fetchScopeTarget('tok', { id: 'proj-123', name: 'CORSIA', color: '#7c5cff' });

		const urls = vi.mocked(globalThis.fetch).mock.calls.map((call) => String(call[0]));
		expect(urls[0]).toMatch(/^\/api\/corsia-agent\?/);
		expect(urls[1]).toMatch(/^\/api\/conductor\?.*&scope=proj-123$/);
	});
});

describe('firstRunLanding', () => {
	const pending = { kind: 'target', target: { thread_id: 'cond-w' }, firstRunPending: true } as const;
	const settled = { ...pending, firstRunPending: false } as const;
	const home = { scope: null, activeThreadId: null, landed: false };

	it('lands on the CORSIA agent only while its first run is pending', () => {
		expect(firstRunLanding(pending, home)).toBe('cond-w');
		expect(firstRunLanding(settled, home)).toBeNull();
		expect(firstRunLanding({ kind: 'absent' }, home)).toBeNull();
		expect(firstRunLanding({ kind: 'unknown' }, home)).toBeNull();
	});

	it('never lands twice, inside a project, or over an open thread', () => {
		expect(firstRunLanding(pending, { ...home, landed: true })).toBeNull();
		expect(firstRunLanding(pending, { ...home, activeThreadId: 'other' })).toBeNull();
		const scope: UiScope = { id: 'proj-1', name: 'P', color: '#000' };
		expect(firstRunLanding(pending, { ...home, scope })).toBeNull();
	});
});

describe('landOnFirstRun', () => {
	const pending = { kind: 'target', target: { thread_id: 'cond-w' }, firstRunPending: true } as const;

	afterEach(() => { _resetFirstRunLandingForTests(); });

	it('opens the conductor once per page load', () => {
		const open = vi.fn();
		landOnFirstRun(pending, null, open);
		landOnFirstRun(pending, null, open);
		expect(open).toHaveBeenCalledTimes(1);
		expect(open).toHaveBeenCalledWith('cond-w');
	});

	it('leaves an already open thread alone', () => {
		const open = vi.fn();
		activeThreadId.set('other');
		landOnFirstRun(pending, null, open);
		expect(open).not.toHaveBeenCalled();
	});
});

describe('setupIncomplete', () => {
	const target = (model: string | null) => ({ kind: 'target' as const, firstRunPending: true, target: { thread_id: 'agent', model } });

	it('is true only for a pending first run with no model chosen', () => {
		expect(setupIncomplete(target(null))).toBe(true);
		expect(setupIncomplete(target('ready-model'))).toBe(false);
		expect(setupIncomplete({ ...target(null), firstRunPending: false })).toBe(false);
		expect(setupIncomplete({ kind: 'unknown' })).toBe(false);
		expect(setupIncomplete({ kind: 'absent' })).toBe(false);
	});
});
