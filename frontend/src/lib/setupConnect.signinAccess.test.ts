import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createSetupConnect, SIGNIN_WAIT_MS } from './setupConnect.svelte';

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });

type Check = Record<string, unknown>;
const claude = { source: 'claude-code', name: 'Claude Code', state: 'needs_auth' as const };

/** A service whose sign-in check answers whatever `check()` returns now, and records every call. */
function serve(check: () => Check) {
	const calls: string[] = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
		if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.startsWith('/api/vendor-accounts?')) return json({ accounts: [] });
		if (url === '/api/vendor-accounts' && method === 'POST') return json({ id: 'account-1' });
		if (url === '/api/vendor-accounts/account-1/signin' && method === 'POST') return json({ account_id: 'account-1', status: 'waiting' });
		if (url === '/api/vendor-accounts/account-1/signin/check') return json({ account_id: 'account-1', ...check() });
		return json({ status: 'cancelled' });
	}));
	const checks = () => calls.filter((call) => call.endsWith('/signin/check')).length;
	return { calls, checks };
}

function flowFor(turnOff = vi.fn()) {
	const refreshSources = vi.fn(async () => undefined);
	const flow = createSetupConnect({ token: () => 'session', sources: () => [claude], refreshSources, turnOff });
	return { flow, turnOff, refreshSources };
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('signing in with an account that has no access', () => {
	it('says so in plain words once the first read of the account finds no usable model, and stops checking', async () => {
		let answer: Check = { status: 'completed', outcome: null, bind_reason: null };
		const { checks } = serve(() => answer);
		const { flow } = flowFor();

		await flow.signIn(claude);
		// The sign-in finished but its models are still being read: still waiting, not a success.
		expect(flow.row(claude).meta).toBe('Finish signing in in your browser.');
		answer = { status: 'completed', outcome: 'no_usable_model', bind_reason: null };
		await flow.tick();

		const row = flow.row(claude);
		expect(row.meta).toBe('This Claude account doesn’t include Claude Code. Try another account, or turn it off and use another option.');
		expect(row.error).toBe(true);
		expect(row.action?.label).toBe('Try another account');
		expect(row.more?.map((item) => item.label)).toEqual(['Turn off']);
		const settled = checks();
		await flow.tick();
		expect(checks()).toBe(settled);
	});

	it('gives Codex the same handling in its own words', async () => {
		serve(() => ({ status: 'completed', outcome: 'no_usable_model' }));
		const codex = { source: 'codex', name: 'Codex', state: 'needs_auth' as const };
		const flow = createSetupConnect({ token: () => 'session', sources: () => [codex], refreshSources: async () => undefined });
		await flow.signIn(codex);
		expect(flow.row(codex).meta).toBe('This ChatGPT account doesn’t include Codex. Try another account, or turn it off and use another option.');
	});

	it('clears the row for an account that works', async () => {
		serve(() => ({ status: 'completed', outcome: 'ready' }));
		const { flow, refreshSources } = flowFor();
		await flow.signIn(claude);
		expect(refreshSources).toHaveBeenCalled();
		expect(flow.row(claude).meta).toBe('Uses your Claude subscription.');
	});
});

describe('a sign-in the browser never returns from', () => {
	it('says the sign-in didn’t finish and names the usual cause when the service gave up waiting', async () => {
		serve(() => ({ status: 'failed', cause: 'signin_timed_out', ref: 'signin-0123456789abcdef' }));
		const { flow } = flowFor();
		await flow.signIn(claude);
		const row = flow.row(claude);
		expect(row.meta).toBe('Didn’t finish signing in to Claude Code. Usually the account doesn’t include Claude Code, and your browser stops on a page from Anthropic instead of returning here. Try another account, or turn it off and use another option.');
		expect(row.action?.label).toBe('Try another account');
		expect(row.more?.map((item) => item.label)).toEqual(['Turn off']);
		expect(row.reportable).toBeUndefined();
	});

	it('stops checking and cancels the vendor login once the wait is over, even if the service never answers', async () => {
		const { calls, checks } = serve(() => ({ status: 'waiting' }));
		const { flow } = flowFor();
		await flow.signIn(claude);
		expect(flow.row(claude).meta).toBe('Finish signing in in your browser.');
		await flow.tick();
		const before = checks();

		vi.setSystemTime(Date.now() + SIGNIN_WAIT_MS + 1000);
		await flow.tick();

		expect(calls).toContain('DELETE /api/vendor-accounts/account-1/signin');
		expect(flow.row(claude).meta).toContain('Didn’t finish signing in to Claude Code.');
		await flow.tick();
		await flow.tick();
		expect(checks()).toBe(before + 1);
		expect(flow.active).toBe(false);
	});

	it('never polls forever: a service that stops answering ends in the same row', async () => {
		serve(() => { throw new TypeError('Failed to fetch'); });
		const { flow } = flowFor();
		await flow.signIn(claude);
		vi.setSystemTime(Date.now() + SIGNIN_WAIT_MS + 1000);
		await flow.tick();
		expect(flow.row(claude).meta).toContain('Didn’t finish signing in to Claude Code.');
	});
});

describe('the two ways out of a sign-in that can’t finish', () => {
	async function failedFlow() {
		const { calls } = serve(() => ({ status: 'failed', cause: 'signin_timed_out' }));
		const made = flowFor();
		await made.flow.signIn(claude);
		return { ...made, calls };
	}

	it('Try another account restarts the sign-in', async () => {
		const { flow, calls } = await failedFlow();
		const starts = () => calls.filter((call) => call === 'POST /api/vendor-accounts/account-1/signin').length;
		expect(starts()).toBe(1);
		flow.row(claude).action?.run();
		await vi.waitFor(() => expect(starts()).toBe(2));
	});

	it('Turn off withdraws the tool and clears the row', async () => {
		const { flow, turnOff } = await failedFlow();
		flow.row(claude).more?.[0].run();
		expect(turnOff).toHaveBeenCalledWith('claude-code');
		expect(flow.row(claude).meta).toBe('Uses your Claude subscription.');
		expect(flow.row(claude).error).toBeUndefined();
	});
});
