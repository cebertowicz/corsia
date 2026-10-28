import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ModelSource } from './conductorSources';
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
		if (url === '/api/vendor-accounts/account-1/signin/check') {
			if (check.status === 'missing') return new Response(JSON.stringify({ detail: 'no sign-in is in progress for this account' }), { status: 409 });
			return json({ account_id: 'account-1', ...check });
		}
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
	it('says what the browser may have shown, keeps the failure reference and offers a new sign-in or turning the tool off', async () => {
		serve({ status: 'failed', cause: 'signin_timed_out', ref: 'ref-7' });
		const flow = flowFor(claude);
		await flow.signIn(claude);
		const row = flow.row(claude);
		expect(row.meta).toBe('Couldn’t finish signing in to Claude Code. If your browser said this account doesn’t include Claude Code, sign in with another account or turn Claude Code off. Otherwise, sign in again.');
		expect(row.error).toBe(true);
		expect(row.reference).toBe('ref-7');
		expect(row.action?.label).toBe('Sign in');
		expect(row.canTurnOff).toBe(true);
	});

	it('says the same for Codex, and signing in again starts a new sign-in', async () => {
		const calls = serve({ status: 'failed', cause: 'signin_timed_out', ref: 'ref-8' });
		const flow = flowFor(codex);
		await flow.signIn(codex);
		expect(flow.row(codex).meta).toBe('Couldn’t finish signing in to Codex. If your browser said this account doesn’t include Codex, sign in with another account or turn Codex off. Otherwise, sign in again.');
		const starts = () => calls.filter((call) => call === 'POST /api/vendor-accounts/account-1/signin').length;
		expect(starts()).toBe(1);
		flow.row(codex).action?.run();
		await vi.waitFor(() => expect(starts()).toBe(2));
	});
});

const account = (state: string) => ({ account_id: `a-${state}`, label: 'me@example.com', label_verified: true, identified: true, state, resets_at: null, corsia_agent_binding: false });
const signedIn = (source: typeof claude, ...states: string[]) => ({ ...source, state: 'ready' as const, accounts: states.map(account) }) as unknown as ModelSource;

describe('a completed sign-in while models are checked', () => {
	it.each([claude, codex])('keeps $name busy until the outcome and refresh settle', async (source) => {
		const check: Record<string, unknown> = { status: 'completed', outcome: null };
		serve(check);
		let current: ModelSource = source;
		const refresh = vi.fn(async () => { current = signedIn(source, 'ready'); });
		const flow = createSetupConnect({ token: () => 'session', sources: () => [current], refreshSources: refresh });
		await flow.signIn(source);
		expect(flow.active).toBe(true);
		expect(flow.row(current)).toEqual({ meta: `Checking your ${source === claude ? 'Claude' : 'ChatGPT'} models…`, busy: true, spinning: true });
		expect(refresh).not.toHaveBeenCalled();
		check.outcome = 'ready';
		await flow.tick();
		expect(refresh).toHaveBeenCalledOnce();
		expect(flow.active).toBe(false);
		expect(flow.row(current).meta).toBe('Ready · me@example.com');
	});

	it('stops polling and refreshes when the backend has lost the sign-in', async () => {
		const check: Record<string, unknown> = { status: 'completed', outcome: null };
		const calls = serve(check);
		let current: ModelSource = claude;
		const refresh = vi.fn(async () => { current = signedIn(claude, 'ready'); });
		const flow = createSetupConnect({ token: () => 'session', sources: () => [current], refreshSources: refresh });
		await flow.signIn(claude);
		check.status = 'missing';
		await flow.tick();
		expect(refresh).toHaveBeenCalledOnce();
		expect(flow.active).toBe(false);
		expect(flow.row(current).meta).toBe('Ready · me@example.com');
		await flow.tick();
		expect(calls.filter((call) => call.endsWith('/signin/check'))).toHaveLength(2);
	});

	it('renders the existing no-model state after the outcome and refresh', async () => {
		const check: Record<string, unknown> = { status: 'completed', outcome: null };
		serve(check);
		let current: ModelSource = claude;
		const flow = createSetupConnect({ token: () => 'session', sources: () => [current], refreshSources: async () => { current = signedIn(claude, 'no_usable_model'); } });
		await flow.signIn(claude);
		check.outcome = 'no_usable_model';
		await flow.tick();
		expect(flow.row(current).meta).toBe('This Claude account has no models for Claude Code. Sign in with another account, or turn Claude Code off.');
		expect(flow.row(current).error).toBe(true);
	});
});

describe('an account the service records as signed in with no usable model', () => {
	it('says the account does not include the tool, from the record alone, and offers a new sign-in or turning it off', () => {
		const flow = flowFor(claude);
		const row = flow.row(signedIn(claude, 'no_usable_model'));
		expect(row.meta).toBe('This Claude account has no models for Claude Code. Sign in with another account, or turn Claude Code off.');
		expect(row.error).toBe(true);
		expect(row.action?.label).toBe('Sign in');
		expect(row.canTurnOff).toBe(true);
	});

	it('names Codex and the ChatGPT account for Codex', () => {
		const flow = flowFor(codex);
		expect(flow.row(signedIn(codex, 'no_usable_model', 'signed_out')).meta).toBe('This ChatGPT account has no models for Codex. Sign in with another account, or turn Codex off.');
	});

	it('leaves a working, limited or still-unchecked account to its own row', () => {
		const flow = flowFor(claude);
		expect(flow.row(signedIn(claude, 'no_usable_model', 'ready')).meta).toBe('Ready · me@example.com');
		expect(flow.row(signedIn(claude, 'no_usable_model', 'limited')).meta).not.toContain('has no models for');
		expect(flow.row(signedIn(claude, 'no_usable_model', 'unchecked')).meta).not.toContain('has no models for');
	});
});
