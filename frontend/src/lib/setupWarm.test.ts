import { afterEach, describe, expect, it, vi } from 'vitest';
import { createSetupWarm } from './setupWarm.svelte';
import type { SetupModel } from './setupModels';
import { emit } from './ws/events';

const local = { id: 'local', litellm_model: 'omlx@test/model', connection: 'omlx', backend: 'network', status: 'ready' } as SetupModel;
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
afterEach(() => { vi.unstubAllGlobals(); });

describe('setup local warm', () => {
	it('starts on readiness before finishing, joins once and never chooses a model', async () => {
		let release!: (response: Response) => void;
		const requests: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input); requests.push(`${init?.method ?? 'GET'} ${url}`);
			if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'agent', model: null }, first_run_pending: true });
			return new Promise<Response>((resolve) => { release = resolve; });
		}));
		const warm = createSetupWarm();
		warm.start([local], 'session');
		await vi.waitFor(() => expect(release).toBeTypeOf('function'));
		expect(warm.running).toBe(true);
		let finished = false;
		const completion = warm.finish().then(() => { finished = true; });
		await Promise.resolve();
		expect(finished).toBe(false);
		warm.start([local], 'session');
		release(json({ ok: true }));
		await completion;
		expect(warm.running).toBe(false);
		expect(warm.percent).toBeNull();
		expect(requests).toHaveLength(2);
		expect(requests[1]).toContain('/agent/warm?');
		expect(requests[1]).toContain('model=omlx%40test%2Fmodel');
		expect(requests[1]).toContain('wait=true');
		expect(requests.some((request) => request.includes('probe_only'))).toBe(false);
		expect(requests.some((request) => request.startsWith('PATCH') || request.startsWith('PUT'))).toBe(false);
		warm.destroy();
	});

	it.each(['failure', 'unreachable'])('finishes normally when warm is %s', async (outcome) => {
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'agent' }, first_run_pending: true });
			if (outcome === 'unreachable') throw new TypeError('Failed to fetch');
			return json({ ok: false });
		}));
		const warm = createSetupWarm();
		warm.start([local], 'session');
		await warm.finish();
		expect(warm.running).toBe(false);
		expect(warm.percent).toBeNull();
		warm.destroy();
	});

	it('does no work without a ready local model or a pending unbound agent', async () => {
		const fetcher = vi.fn(async () => json({ target: { thread_id: 'agent', model: 'already-picked' }, first_run_pending: true }));
		vi.stubGlobal('fetch', fetcher);
		const warm = createSetupWarm();
		warm.start([{ ...local, status: 'unavailable' }], 'session');
		warm.start([local], null);
		expect(fetcher).not.toHaveBeenCalled();
		warm.start([local], 'session');
		await warm.finish();
		expect(fetcher).toHaveBeenCalledTimes(1);
		warm.destroy();
	});

	it('reports only matching provider progress while the warm is running', async () => {
		let release!: (response: Response) => void;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'agent' }, first_run_pending: true });
			return new Promise<Response>((resolve) => { release = resolve; });
		}));
		const warm = createSetupWarm();
		warm.start([local], 'session');
		await vi.waitFor(() => expect(release).toBeTypeOf('function'));
		emit('thread_warm_progress', { type: 'thread_warm_progress', thread_id: 'other', model: local.litellm_model, progress: { total_blocks: 10, ready_blocks: 9 } });
		emit('thread_warm_progress', { type: 'thread_warm_progress', thread_id: 'agent', model: local.litellm_model, progress: { total_blocks: 10, ready_blocks: 4 } });
		expect(warm.percent).toBe(40);
		expect(warm.running).toBe(true);
		release(json({ ok: true }));
		await warm.finish();
		expect(warm.percent).toBe(40);
		warm.destroy();
	});

	it('shows no percentage when provider progress lacks a usable count', async () => {
		let release!: (response: Response) => void;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'agent' }, first_run_pending: true });
			return new Promise<Response>((resolve) => { release = resolve; });
		}));
		const warm = createSetupWarm();
		warm.start([local], 'session');
		await vi.waitFor(() => expect(release).toBeTypeOf('function'));
		emit('thread_warm_progress', { type: 'thread_warm_progress', thread_id: 'agent', model: local.litellm_model, progress: { total_blocks: 0, ready_blocks: 0 } });
		expect(warm.percent).toBeNull();
		emit('thread_warm_progress', { type: 'thread_warm_progress', thread_id: 'agent', model: local.litellm_model, progress: { total_blocks: 10, ready_blocks: null } });
		expect(warm.percent).toBeNull();
		release(json({ ok: true }));
		await warm.finish();
		warm.destroy();
	});
});
