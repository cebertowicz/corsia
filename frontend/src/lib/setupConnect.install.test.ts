import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ModelSource } from './conductorSources';
import { createSetupConnect } from './setupConnect.svelte';

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

describe('setup installation before sign-in', () => {
	it('waits for the install start request before reading its status', async () => {
		let source: ModelSource | undefined, state = 'idle', statusReads = 0;
		let resolveStart: (value: Response) => void = () => undefined;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			if (String(input).includes('/install') && init?.method === 'POST') return new Promise<Response>((resolve) => { resolveStart = resolve; });
			if (String(input).includes('/install')) { statusReads++; return json({ state, path_shadowed_by: null }); }
			return json({});
		}));
		const flow = createSetupConnect({ token: () => 'fixture', sources: () => source ? [source] : [], refreshSources: async () => {
			if (!source) { source = { source: 'claude-code', name: 'Claude Code', state: 'not_installed' }; flow.sourcesChanged(); }
		} });
		flow.pick('claude-code');
		await flow.tick();
		expect(statusReads).toBe(0);
		expect(flow.row(source!).meta).toBe('Installing Claude Code…');
		expect(flow.attempts['claude-code']).toEqual({ attempt: 'none' });
		await flow.tick();
		expect(statusReads).toBe(0);
		state = 'running'; resolveStart(json({ state: 'running' }));
		await vi.waitFor(() => expect(statusReads).toBe(1));
		expect(flow.row(source!).meta).toBe('Installing Claude Code…');
		state = 'done'; source = { ...source!, state: 'needs_auth' };
		await flow.tick();
		expect(flow.row(source).action?.label).toBe('Sign in');
		expect(flow.attempts['claude-code']).toEqual({ attempt: 'installed' });
	});

	it.each([['claude-code', 'Claude Code'], ['codex', 'Codex']] as const)('%s offers sign-in only after installation', async (vendor, name) => {
		let source: ModelSource = { source: vendor, name, state: 'not_installed' };
		let installState = 'running'; const calls: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
			if (url.includes('/install')) return json({ state: installState, path_shadowed_by: null });
			if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'fixture' } });
			if (url.startsWith('/api/vendor-accounts?')) return json({ accounts: [] });
			if (url === '/api/vendor-accounts' && method === 'POST') return json({ id: 'account-1' });
			if (url.includes('/signin')) return json({ status: 'waiting' });
			return json({});
		}));
		const flow = createSetupConnect({ token: () => 'fixture', sources: () => [source], refreshSources: async () => undefined });
		await flow.install(source);
		expect(flow.row(source)).toMatchObject({ meta: `Installing ${name}…`, busy: true, spinning: true });
		expect(flow.row(source).action).toBeUndefined();
		await flow.signIn(source);
		installState = 'done'; source = { ...source, state: 'needs_auth' };
		await flow.tick();
		expect(calls.some((call) => call.includes('/signin'))).toBe(false);
		expect(flow.row(source).action?.label).toBe('Sign in');
		flow.row(source).action?.run();
		await vi.waitFor(() => expect(calls.some((call) => call.includes('/signin'))).toBe(true));
	});
});
