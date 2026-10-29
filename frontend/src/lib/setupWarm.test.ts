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
		expect(requests[1]).toContain('progress=true');
		expect(requests.some((request) => request.includes('probe_only'))).toBe(false);
		expect(requests.some((request) => request.startsWith('PATCH') || request.startsWith('PUT'))).toBe(false);
		warm.destroy();
	});

	it.each(['a server error', 'a failure', 'unreachable', 'non-JSON'])('records why and finishes normally when warm is %s', async (outcome) => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'agent' }, first_run_pending: true });
			if (String(input).includes('skip_reason=')) return json({ ok: true });
			if (outcome === 'unreachable') throw new TypeError('Failed to fetch');
			if (outcome === 'a server error') return new Response('Internal Server Error', { status: 500 });
			if (outcome === 'non-JSON') return new Response('invalid JSON');
			return json({ ok: false, message: 'model not loaded' });
		}));
		const warm = createSetupWarm();
		warm.start([local], 'session');
		await warm.finish();
		expect(warm.running).toBe(false);
		expect(warm.percent).toBeNull();
		expect(warn).toHaveBeenCalledTimes(1);
		await vi.waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('skip_reason=warm_failed'))).toHaveLength(1));
		warn.mockRestore();
		warm.destroy();
	});

	it('does no work without a session', async () => {
		const fetcher = vi.fn();
		vi.stubGlobal('fetch', fetcher);
		const warm = createSetupWarm();
		warm.start([local], null);
		await warm.finish();
		expect(fetcher).not.toHaveBeenCalled();
		warm.destroy();
	});

	it.each([
		['no_candidate', [{ ...local, status: 'unavailable' }], null, true],
		['agent_has_model', [local], 'already-picked', true],
		['first_run_not_pending', [local], null, false],
	])('reports %s without waiting for the log call', async (reason, catalog, model, pending) => {
		const fetcher = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'agent', model }, first_run_pending: pending });
			return new Promise<Response>(() => {});
		});
		vi.stubGlobal('fetch', fetcher);
		const warm = createSetupWarm();
		warm.start(catalog as SetupModel[], 'session');
		await warm.finish();
		await vi.waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url).includes(`skip_reason=${reason}`))).toHaveLength(1));
		expect(warm.running).toBe(false);
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
