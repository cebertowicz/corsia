import { afterEach, describe, expect, it, vi } from 'vitest';

import { createSetupConnect } from './setupConnect.svelte';

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

afterEach(() => { vi.unstubAllGlobals(); });

describe('setup connect stop', () => {
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
		['a refusal the service sent', () => Promise.resolve(new Response('{}', { status: 409 })), 'Couldn’t finish signing in to Codex. Sign in again, or save a report.'],
		['no answer at all', () => Promise.reject(new TypeError('Failed to fetch')), 'CORSIA didn’t answer while starting the Codex sign-in. Sign in again.'],
	])('names the sign-in start failure only as far as it is known: %s', async (_case, answer, line) => {
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
