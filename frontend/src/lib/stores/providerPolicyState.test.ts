import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createProviderPolicyState } from './providerPolicyState.svelte';
import { token } from '$lib/stores/auth';

const entry = (revision: number, disabled: string[] = []) => ({
	provider_key: 'codex', capabilities: { units: 'accounts', rollover: true }, rollover: true, order: null, disabled, revision,
	units: [{ id: 'a', label: 'A', enabled: !disabled.includes('a') }, { id: 'b', label: 'B', enabled: true }],
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const puts = () => vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === 'PUT').map(([, init]) => JSON.parse(String(init!.body)));

beforeEach(() => token.set('test-token'));
afterEach(() => {
	token.set(null);
	vi.unstubAllGlobals();
});

describe('providerPolicyState failures', () => {
	it('refreshes after a revision conflict so the retry sends the current revision', async () => {
		let revision = 1;
		let conflictOnce = true;
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PUT') {
				if (conflictOnce) { conflictOnce = false; revision = 2; return json({ detail: 'provider policy revision changed; refresh before acting' }, 409); }
				return json(entry(revision + 1, JSON.parse(String(init.body)).disabled));
			}
			return json({ scope: 'workspace', providers: [entry(revision)] });
		}));
		const state = createProviderPolicyState();
		await state.load();
		const target = 'unit:codex:a';

		await state.save(state.policy!.providers[0], { disabled: ['a'] }, target);
		expect(state.failureText(target)).toBe('Couldn’t save this change. It changed elsewhere and now shows the latest setting.');
		expect(state.hasFailure(target)).toBe(true);
		expect(state.policy!.providers[0].revision).toBe(2);
		expect(state.loading).toBe(false);

		await state.save(state.policy!.providers[0], { disabled: ['a'] }, target);
		expect(puts().map((body) => body.expected_revision)).toEqual([1, 2]);
		expect(state.failureText(target)).toBeNull();
	});

	it('says to check the connection when the server was not reached', async () => {
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PUT') throw new TypeError('Failed to fetch');
			return json({ scope: 'workspace', providers: [entry(1)] });
		}));
		const state = createProviderPolicyState();
		await state.load();
		await state.save(state.policy!.providers[0], { rollover: false }, 'rollover:codex');
		expect(state.failureText('rollover:codex')).toBe('Couldn’t save this change. Check your connection and try again.');
	});

	it('shows the server’s reason and keeps other targets clear', async () => {
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PUT'
			? json({ detail: 'Rollover is not available for this provider.' }, 400)
			: json({ scope: 'workspace', providers: [entry(1)] })));
		const state = createProviderPolicyState();
		await state.load();
		await state.save(state.policy!.providers[0], { rollover: false }, 'rollover:codex');
		expect(state.failureText('rollover:codex')).toBe('Couldn’t save this change. Rollover is not available for this provider.');
		expect(state.failureText('unit:codex:a')).toBeNull();
		expect(state.hasControlFailure()).toBe(true);
	});
});
