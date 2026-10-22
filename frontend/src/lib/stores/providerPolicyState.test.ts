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
				if (conflictOnce) { conflictOnce = false; revision = 2; return json({ detail: 'provider policy revision changed; refresh before acting', code: 'policy_changed' }, 409); }
				return json(entry(revision + 1, JSON.parse(String(init.body)).disabled));
			}
			return json({ scope: 'workspace', providers: [entry(revision)] });
		}));
		const state = createProviderPolicyState();
		await state.load();
		const target = 'unit:codex:a';

		await state.save(state.policy!.providers[0], { disabled: ['a'] }, target);
		expect(state.failureText(target)).toBe('Couldn’t save this change. This shows the latest setting; try again to change it.');
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

	it('names a failure of unknown cause generically, with no re-read and no server text, whatever its status', async () => {
		for (const [status, code] of [[422, 'account_conflict'], [409, undefined]] as const) {
			vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PUT'
				? json({ detail: 'policy account must belong to this provider', code }, status)
				: json({ scope: 'workspace', providers: [entry(1)] })));
			const state = createProviderPolicyState();
			await state.load();
			await state.save(state.policy!.providers[0], { rollover: false }, 'rollover:codex');
			expect(state.failureText('rollover:codex')).toBe('Couldn’t save this change. Try again.');
			expect(state.failureText('unit:codex:a')).toBeNull();
			expect(state.error).toBe('');
			expect(vi.mocked(fetch).mock.calls.filter(([, init]) => !init?.method)).toHaveLength(1);
		}
	});

	it('says the project is gone, without "try again" or a re-read, when the server reports project_not_found', async () => {
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PUT'
			? json({ detail: 'project does not exist', code: 'project_not_found' }, 404)
			: json({ scope: 'workspace', providers: [entry(1)] })));
		const state = createProviderPolicyState();
		await state.load();
		await state.save(state.policy!.providers[0], { rollover: false }, 'rollover:codex');
		expect(state.failureText('rollover:codex')).toBe('Couldn’t save this change. This project doesn’t exist; choose another project.');
		expect(vi.mocked(fetch).mock.calls.filter(([, init]) => !init?.method)).toHaveLength(1);
	});

	it('says to check the connection, not "shows the latest setting", when a write committed but its response was lost', async () => {
		let revision = 1;
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PUT') { revision = 2; throw new TypeError('Failed to fetch'); }
			return json({ scope: 'workspace', providers: [entry(revision)] });
		}));
		const state = createProviderPolicyState();
		await state.load();
		await state.save(state.policy!.providers[0], { rollover: false }, 'rollover:codex');
		expect(state.failureText('rollover:codex')).toBe('Couldn’t save this change. Check your connection and try again.');
		expect(state.policy!.providers[0].revision).toBe(2);
	});

	it('says to check the connection, and keeps the error for a reload, when the re-read after a failed save also fails', async () => {
		let reads = 0;
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PUT') return json({ detail: 'provider policy revision changed; refresh before acting', code: 'policy_changed' }, 409);
			if (reads++ === 0) return json({ scope: 'workspace', providers: [entry(1)] });
			throw new TypeError('Failed to fetch');
		}));
		const state = createProviderPolicyState();
		await state.load();
		await state.save(state.policy!.providers[0], { rollover: false }, 'rollover:codex');
		expect(state.failureText('rollover:codex')).toBe('Couldn’t save this change. Check your connection and try again.');
		expect(state.error).toBe('Couldn’t load providers. Check your connection.');
		expect(state.policy!.providers[0].revision).toBe(1);
	});

	it('names a failed read as a load, never with the route’s or a save’s wording', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => new Response('oops', { status: 500 })));
		const state = createProviderPolicyState();
		await state.load();
		expect(state.error).toBe('Couldn’t load providers.');
	});

	it('names the same causes for the project scope switch', async () => {
		let failure: { detail: string; code: string; status: number } = { detail: 'project override revision changed', code: 'policy_changed', status: 409 };
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PUT'
			? json({ detail: failure.detail, code: failure.code }, failure.status)
			: json({ scope: 'project', use_workspace: false, override_revision: 1, providers: [entry(1)] })));
		const state = createProviderPolicyState('p1');
		await state.load();
		await state.toggleScope(true);
		expect(state.failureText('scope')).toBe('Couldn’t save this change. This shows the latest setting; try again to change it.');
		failure = { detail: 'project does not exist', code: 'project_not_found', status: 404 };
		await state.toggleScope(true);
		expect(state.failureText('scope')).toBe('Couldn’t save this change. This project doesn’t exist; choose another project.');
		failure = { detail: 'rollover is not available for this provider', code: 'account_conflict', status: 422 };
		await state.toggleScope(true);
		expect(state.failureText('scope')).toBe('Couldn’t save this change. Try again.');
	});
});

describe('providerPolicyState patches are built from the server’s units', () => {
	// `x` was removed: it lingers in the raw arrays and `units` leaves it out; the server drops it on save.
	const staleEntry = (over: object = {}) => ({
		...entry(1), order: ['x', 'b', 'a'], disabled: ['x'],
		units: [{ id: 'b', label: 'B', enabled: true }, { id: 'a', label: 'A', enabled: true }],
		...over,
	});
	const load = async (provider: object) => {
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PUT'
			? json({ ...provider, revision: 2 })
			: json({ scope: 'workspace', providers: [provider] })));
		const state = createProviderPolicyState();
		await state.load();
		return state;
	};

	it('moves an account that is in units but not in the saved order', async () => {
		const state = await load(staleEntry({ order: ['b'], disabled: [], units: [{ id: 'b', label: 'B', enabled: true }, { id: 'a', label: 'A', enabled: true }] }));
		state.move(state.policy!.providers[0], 'a', -1);
		await vi.waitFor(() => expect(puts()).toHaveLength(1));
		expect(puts()[0].order).toEqual(['a', 'b']);
	});

	it('sends nothing for a move off either end', async () => {
		const state = await load(staleEntry());
		state.move(state.policy!.providers[0], 'a', 1);
		state.move(state.policy!.providers[0], 'b', -1);
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(puts()).toEqual([]);
		expect(state.isPending('order:codex:a')).toBe(false);
	});

	it('builds a toggle or a move from units, so it names no removed account; other saves send the entry as it is', async () => {
		const state = await load(staleEntry());
		const provider = () => state.policy!.providers[0];
		state.setEnabled(provider(), 'a', false);
		await vi.waitFor(() => expect(puts()).toHaveLength(1));
		expect(puts()[0].disabled).toEqual(['a']);
		await state.save(provider(), { rollover: false }, 'rollover:codex');
		expect(puts()[1]).toMatchObject({ order: ['x', 'b', 'a'], disabled: ['x'], rollover: false });
	});

	it('keeps an order that was never saved as null', async () => {
		const state = await load(staleEntry({ order: null, disabled: [] }));
		await state.save(state.policy!.providers[0], { rollover: false }, 'rollover:codex');
		expect(puts()[0].order).toBeNull();
	});
});
