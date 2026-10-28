import { afterEach, describe, expect, it, vi } from 'vitest';

import { createSetupConnect } from './setupConnect.svelte';

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

afterEach(() => { vi.unstubAllGlobals(); });

describe('setup connect stop', () => {
	it('drops a pending choice when unpicked and never installs an already detected CLI', async () => {
		let sources: Array<{ source: string; name: string; state: 'not_installed' | 'ready' }> = [];
		const fetchSpy = vi.fn(); vi.stubGlobal('fetch', fetchSpy);
		const flow = createSetupConnect({ token: () => 'session', sources: () => sources, refreshSources: async () => undefined });
		flow.pick('claude-code'); await flow.stop('claude-code');
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }];
		flow.sourcesChanged();
		sources = [{ source: 'codex', name: 'Codex', state: 'ready' }];
		flow.pick('codex'); flow.sourcesChanged();
		expect(fetchSpy).not.toHaveBeenCalled();
	});

	it('cancels a sign-in that was still starting when its tool was unpicked', async () => {
		const calls: string[] = [];
		let answerStart: (response: Response) => void = () => undefined;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
			if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
			if (url.startsWith('/api/vendor-accounts?')) return json({ accounts: [] });
			if (url === '/api/vendor-accounts' && method === 'POST') return json({ id: 'account-1' });
			if (url === '/api/vendor-accounts/account-1/signin' && method === 'POST') return new Promise<Response>((resolve) => { answerStart = resolve; });
			return json({ status: 'cancelled' });
		}));
		const source = { source: 'codex', name: 'Codex', state: 'needs_auth' as const };
		const flow = createSetupConnect({ token: () => 'session', sources: () => [source], refreshSources: async () => undefined });

		const signing = flow.signIn(source);
		await vi.waitFor(() => expect(calls).toContain('POST /api/vendor-accounts/account-1/signin'));
		await flow.stop('codex');
		answerStart(json({ account_id: 'account-1', status: 'waiting' }));
		await signing;

		expect(calls).toContain('DELETE /api/vendor-accounts/account-1/signin');
		expect(calls.some((call) => call.endsWith('/signin/check'))).toBe(false);
		expect(flow.active).toBe(false);
	});

	it.each([
		['the account request is refused', '/api/vendor-accounts', () => Promise.resolve(new Response('{}', { status: 500 }))],
		['the account request gets no answer', '/api/vendor-accounts', () => Promise.reject(new TypeError('Failed to fetch'))],
		['the CORSIA agent is missing', '/api/corsia-agent', () => Promise.resolve(json({}))],
	] as const)('leaves an unpicked tool cleared when its sign-in start then fails: %s', async (_case, held, answer) => {
		let release: () => void = () => undefined;
		const gate = new Promise<void>((resolve) => { release = resolve; });
		const calls: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
			const hold = url.startsWith(held) && (held !== '/api/vendor-accounts' || (url === held && method === 'POST'));
			if (hold) { await gate; return answer(); }
			if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
			if (url.startsWith('/api/vendor-accounts?')) return json({ accounts: [] });
			return json({});
		}));
		const source = { source: 'codex', name: 'Codex', state: 'needs_auth' as const };
		const flow = createSetupConnect({ token: () => 'session', sources: () => [source], refreshSources: async () => undefined });

		const signing = flow.signIn(source);
		await vi.waitFor(() => expect(calls.some((call) => call.includes(held))).toBe(true));
		await flow.stop('codex');
		release();
		await signing;

		expect(flow.row(source)).toMatchObject({ meta: 'Uses your ChatGPT subscription.' });
		expect(flow.row(source).error).toBeUndefined();
		expect(flow.attempts.codex).toEqual({ attempt: 'none' });
	});

	it.each([
		['a refusal the service sent', () => Promise.resolve(new Response('{}', { status: 409 })), 'Sign-in didn’t finish. Press Sign in to try again.'],
		['no answer at all', () => Promise.reject(new TypeError('Failed to fetch')), 'Sign-in didn’t finish. Press Sign in to try again.'],
	])('keeps a sign-in start failure to one line that names no internals: %s', async (_case, answer, line) => {
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input), method = init?.method ?? 'GET';
			if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
			if (url.startsWith('/api/vendor-accounts?')) return json({ accounts: [] });
			if (url === '/api/vendor-accounts' && method === 'POST') return json({ id: 'account-1' });
			if (url === '/api/vendor-accounts/account-1/signin' && method === 'POST') return answer();
			return json({});
		}));
		const source = { source: 'codex', name: 'Codex', state: 'needs_auth' as const };
		const flow = createSetupConnect({ token: () => 'session', sources: () => [source], refreshSources: async () => undefined });

		await flow.signIn(source);

		expect(flow.row(source).meta).toBe(line);
	});
});
