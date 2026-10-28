import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	_resetFirstRunLandingForTests,
	conductorScopeParam,
	fetchConductor,
	fetchCorsiaAgent,
	fetchScopeTarget,
	firstRunLanding,
	landOnFirstRun,
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
import { composerSelectionFor } from '$lib/stores/composerSelection';
import { _resetUnlistedThreadRowsForTests } from '$lib/stores/threadRow';
import { applyPendingThreadModelSwitch } from '$lib/stores/threadModelSwitch';
import { handleSessionUpdated } from '$lib/ws/handlersSession';

const originalFetch = globalThis.fetch;

afterEach(() => {
	vi.restoreAllMocks();
	globalThis.fetch = originalFetch;
	activeThreadId.set(null);
	activeModelId.set(null);
	corsiaAgentId.set(null);
	_resetUnlistedThreadRowsForTests();
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

	it('shows the switch the server still holds after a reload, and clears it once it ended', async () => {
		const read = (fields: Record<string, unknown>) => {
			globalThis.fetch = vi.fn(async () => ({
				ok: true,
				json: async () => ({ thread_id: 'cond-1', target: { thread_id: 'cond-1', ...fields } }),
			})) as unknown as typeof fetch;
			return fetchCorsiaAgent('tok');
		};
		const switching = () => get(composerSelectionFor)('cond-1').switching;

		await read({ pending_model: 'm/b', switch_status: 'preparing', switch_error: null, switch_operation_id: 'op-1' });
		expect(switching()).toMatchObject({ pending_model: 'm/b', switch_status: 'preparing' });

		await read({ pending_model: 'm/b', switch_status: 'failed', switch_error: 'No room', switch_operation_id: 'op-1' });
		expect(switching()).toMatchObject({ switch_status: 'failed', switch_error: 'No room' });

		// The commit landed while the page was disconnected: the read carries no switch.
		await read({ pending_model: null, switch_status: null });
		expect(switching()?.pending_model).toBeNull();
		expect(switching()?.switch_status).toBeNull();
	});

	describe('a read that straddles a newer write', () => {
		const switching = () => get(composerSelectionFor)('cond-1').switching;
		// A read whose response is held until the test releases it.
		function heldRead(fields: Record<string, unknown>) {
			let release: () => void = () => {};
			const gate = new Promise<void>((resolve) => { release = resolve; });
			globalThis.fetch = vi.fn(async () => {
				await gate;
				return {
					ok: true,
					json: async () => ({ thread_id: 'cond-1', target: { thread_id: 'cond-1', ...fields } }),
				};
			}) as unknown as typeof fetch;
			return { done: fetchCorsiaAgent('tok'), release };
		}

		it('does not clear a pending switch that a 202 began after the read started', async () => {
			const stale = heldRead({ pending_model: null, switch_status: null });
			applyPendingThreadModelSwitch('cond-1', {
				operation_id: 'op-1', status: 'preparing', pending_model: 'm/b', checkpoint_required: false,
			});
			stale.release();
			await stale.done;
			expect(switching()).toMatchObject({ pending_model: 'm/b', switch_status: 'preparing' });
		});

		it('does not bring back a switch that a commit frame finished after the read started', async () => {
			applyPendingThreadModelSwitch('cond-1', {
				operation_id: 'op-1', status: 'preparing', pending_model: 'm/b', checkpoint_required: false,
			});
			const stale = heldRead({ pending_model: 'm/b', switch_status: 'preparing', switch_operation_id: 'op-1' });
			handleSessionUpdated({
				thread_id: 'cond-1', model: 'm/b', updated_at: '2026-10-28T12:00:00Z',
				pending_model: null, switch_status: null, switch_error: null, switch_operation_id: null,
			});
			stale.release();
			await stale.done;
			expect(switching()?.pending_model).toBeNull();
			expect(switching()?.switch_status).toBeNull();
		});

		it('keeps the newer of two reads that finish out of order', async () => {
			const older = heldRead({ pending_model: 'm/b', switch_status: 'preparing' });
			const newer = heldRead({ pending_model: null, switch_status: null });
			newer.release();
			await newer.done;
			older.release();
			await older.done;
			expect(switching()?.pending_model).toBeNull();
		});
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

	it('opens the conductor once per page load', async () => {
		const open = vi.fn();
		await landOnFirstRun(pending, null, open);
		await landOnFirstRun(pending, null, open);
		expect(open).toHaveBeenCalledTimes(1);
		expect(open).toHaveBeenCalledWith('cond-w');
	});

	it('does not mark first run landed until the async opener completes', async () => {
		let resolveOpen!: () => void;
		const opening = new Promise<void>((resolve) => { resolveOpen = resolve; });
		const open = vi.fn(() => opening);

		const first = landOnFirstRun(pending, null, open);
		await Promise.resolve();
		const duplicate = landOnFirstRun(pending, null, open);
		expect(open).toHaveBeenCalledTimes(1);

		resolveOpen();
		await Promise.all([first, duplicate]);
		await landOnFirstRun(pending, null, open);
		expect(open).toHaveBeenCalledTimes(1);
	});

	it('logs and retries first-run entry after the opener fails', async () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const open = vi.fn()
			.mockRejectedValueOnce(new Error('first open canceled'))
			.mockResolvedValueOnce(undefined);

		await landOnFirstRun(pending, null, open);
		await landOnFirstRun(pending, null, open);
		expect(warn).toHaveBeenCalledWith('[first run] landing failed; remains retryable');
		expect(open).toHaveBeenCalledTimes(2);
	});

	it('retries when the opener reports no mounted chat surface', async () => {
		const open = vi.fn()
			.mockResolvedValueOnce(false)
			.mockResolvedValueOnce(true);

		await landOnFirstRun(pending, null, open);
		await landOnFirstRun(pending, null, open);
		expect(open).toHaveBeenCalledTimes(2);
	});

	it('retries first-run entry after a synchronous opener failure', async () => {
		const open = vi.fn()
			.mockImplementationOnce(() => { throw new Error('first open canceled'); })
			.mockResolvedValueOnce(undefined);

		await landOnFirstRun(pending, null, open);
		await landOnFirstRun(pending, null, open);
		expect(open).toHaveBeenCalledTimes(2);
	});

	it('leaves an already open thread alone', () => {
		const open = vi.fn();
		activeThreadId.set('other');
		landOnFirstRun(pending, null, open);
		expect(open).not.toHaveBeenCalled();
	});
});
