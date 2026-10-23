import { afterEach, describe, expect, it, vi } from 'vitest';

import { createSetupConnect } from './setupConnect.svelte';

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
const claude = { source: 'claude-code', name: 'Claude Code', state: 'needs_auth' as const };
const codex = { source: 'codex', name: 'Codex', state: 'needs_auth' as const };

/** A service that answers every sign-in check with `check`, and records every call. */
function serve(check: Record<string, unknown>) {
	const calls: string[] = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
		if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.startsWith('/api/vendor-accounts?')) return json({ accounts: [] });
		if (url === '/api/vendor-accounts' && method === 'POST') return json({ id: 'account-1' });
		if (url === '/api/vendor-accounts/account-1/signin' && method === 'POST') return json({ account_id: 'account-1', status: 'waiting' });
		if (url === '/api/vendor-accounts/account-1/signin/check') return json({ account_id: 'account-1', ...check });
		return json({ status: 'cancelled' });
	}));
	return calls;
}

function flowFor(source: typeof claude) {
	const token = () => 'session';
	return createSetupConnect({ token, sources: () => [source], refreshSources: vi.fn(async () => undefined) });
}

afterEach(() => { vi.unstubAllGlobals(); });

describe('a sign-in the browser never returns to', () => {
	it('says the browser did not finish, keeps the failure reference and offers to sign in again', async () => {
		serve({ status: 'failed', cause: 'signin_timed_out', ref: 'ref-7' });
		const flow = flowFor(claude);
		await flow.signIn(claude);
		const row = flow.row(claude);
		expect(row.meta).toBe('Sign-in didn’t finish in your browser. Press Sign in to try again.');
		expect(row.error).toBe(true);
		expect(row.reference).toBe('ref-7');
		expect(row.action?.label).toBe('Sign in');
	});

	it('says the same for Codex, and signing in again starts a new sign-in', async () => {
		const calls = serve({ status: 'failed', cause: 'signin_timed_out', ref: 'ref-8' });
		const flow = flowFor(codex);
		await flow.signIn(codex);
		expect(flow.row(codex).meta).toBe('Sign-in didn’t finish in your browser. Press Sign in to try again.');
		const starts = () => calls.filter((call) => call === 'POST /api/vendor-accounts/account-1/signin').length;
		expect(starts()).toBe(1);
		flow.row(codex).action?.run();
		await vi.waitFor(() => expect(starts()).toBe(2));
	});
});
