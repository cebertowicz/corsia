import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { addVendorAccount, cancelVendorSignin, checkVendorSignin, fetchRemovedVendorAccounts, loadVendorAccounts, refreshVendorModels, recordVendorAccountOverage, removeVendorAccount, renameVendorAccount, setVendorAccountBilling, signOutVendorAccount, startVendorSignin, type VendorAccount, vendorAccounts } from './vendorAccounts';

describe('request ordering', () => {
	const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
	const list = (accounts: VendorAccount[]) => response({ accounts });
	const account = (provider: string): VendorAccount => ({
		id: 'work', provider, name: 'Work', state: 'signed_in', revision: 1, is_default: false, identity: {},
		label: { name: 'Work', email: null, text: 'Work' },
	});
	function deferred() {
		let resolve!: (r: Response) => void;
		const promise = new Promise<Response>((r) => resolve = r);
		return { resolve, promise };
	}
	afterEach(() => { vendorAccounts.set([]); vi.unstubAllGlobals(); });

	it('keeps a confirmed signed-out result when the account reload fails', async () => {
		vendorAccounts.set([account('codex')]);
		vi.stubGlobal('fetch', vi.fn()
			.mockResolvedValueOnce(response({ state: 'signed_out', vendor_signed_in: false, model_refresh_ok: null }))
			.mockResolvedValueOnce(response({ detail: 'inventory unavailable', ref: 'req-7' }, 500)));
		const result = await refreshVendorModels('work');
		expect(result).toMatchObject({ ok: false, signedOut: true });
		expect(result.error).toContain('Reference: req-7');
		expect(get(vendorAccounts)[0]).toMatchObject({ state: 'signed_out', details_stale: true });
	});

	it('reports a failed account reload after a lost login starts', async () => {
		vendorAccounts.set([account('codex')]);
		vi.stubGlobal('fetch', vi.fn()
			.mockResolvedValueOnce(response({ account_id: 'work', status: 'waiting', route: 'browser', cause: null }))
			.mockResolvedValueOnce(response({ detail: 'inventory unavailable' }, 500)));
		const result = await startVendorSignin('work');
		expect(result.ok).toBe(true);
		expect(result.signin?.status).toBe('waiting');
		expect(result.error).toBeTruthy();
	});

	for (const provider of ['codex', 'claude-code']) {
		it(`accepts ordinary current inventory for ${provider}`, async () => {
			const original = account(provider), fresh = { ...original, name: 'Current', revision: 2 };
			vendorAccounts.set([original]);
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(list([fresh])));
			expect((await loadVendorAccounts()).ok).toBe(true);
			expect(get(vendorAccounts)).toEqual([fresh]);
		});
		it(`fresh explicit inventory clears stale receipt details for ${provider}`, async () => {
			const original = { ...account(provider), revision: 0, details_stale: true };
			const fresh = { ...account(provider), revision: 3 };
			vendorAccounts.set([original]);
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(list([fresh])));
			expect((await loadVendorAccounts()).ok).toBe(true);
			expect(get(vendorAccounts)).toEqual([fresh]);
			expect(get(vendorAccounts)[0].details_stale).toBeUndefined();
		});
		it(`honors genuine removal in current inventory for ${provider}`, async () => {
			vendorAccounts.set([account(provider)]);
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(list([])));
			expect((await loadVendorAccounts()).ok).toBe(true);
			expect(get(vendorAccounts)).toEqual([]);
		});
		it.each([true, false])(`overlapping inventory reads retain latest successful request for ${provider}; newer-first=%s`, async (newerFirst) => {
			const original = account(provider), older = { ...original, name: 'Older' }, newer = { ...original, name: 'Newer' };
			vendorAccounts.set([original]);
			const first = deferred(), second = deferred();
			vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
			const read1 = loadVendorAccounts(), read2 = loadVendorAccounts();
			if (newerFirst) {
				second.resolve(list([newer])); expect((await read2).ok).toBe(true);
				first.resolve(list([older])); expect(await read1).toMatchObject({ ok: false, superseded: true });
			} else {
				first.resolve(list([older])); expect((await read1).ok).toBe(true);
				second.resolve(list([newer])); expect((await read2).ok).toBe(true);
			}
			expect(get(vendorAccounts)).toEqual([newer]);
		});
		it(`failed newer read does not invalidate earlier successful inventory for ${provider}`, async () => {
			const original = account(provider), fresh = { ...original, name: 'Loaded' };
			vendorAccounts.set([original]);
			const held = deferred();
			vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(held.promise).mockResolvedValueOnce(response({ detail: 'offline' }, 503)));
			const first = loadVendorAccounts();
			expect((await loadVendorAccounts()).ok).toBe(false);
			held.resolve(list([fresh])); expect((await first).ok).toBe(true);
			expect(get(vendorAccounts)).toEqual([fresh]);
		});
		it(`earlier inventory cannot undo confirmed rename for ${provider}`, async () => {
			const original = account(provider), fresh = { ...original, name: 'Renamed', revision: 2 };
			vendorAccounts.set([original]);
			const held = deferred();
			vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(held.promise).mockResolvedValueOnce(response(fresh)));
			const earlier = loadVendorAccounts();
			expect((await renameVendorAccount(original, 'Renamed')).ok).toBe(true);
			held.resolve(list([original])); expect(await earlier).toMatchObject({ ok: false, superseded: true });
			expect(get(vendorAccounts)).toEqual([fresh]);
		});
		it(`earlier inventory cannot clear revision-conflict recovery for ${provider}`, async () => {
			const original = account(provider);
			vendorAccounts.set([original]);
			const held = deferred();
			vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(held.promise)
				.mockResolvedValueOnce(response({ detail: 'revision changed' }, 409))
				.mockResolvedValueOnce(list([{ ...original, revision: 2 }])));
			const earlier = loadVendorAccounts();
			expect((await renameVendorAccount(original, 'Renamed')).ok).toBe(false);
			expect(get(vendorAccounts)[0].details_stale).toBe(true);
			held.resolve(list([original])); expect(await earlier).toMatchObject({ ok: false, superseded: true });
			expect(get(vendorAccounts)[0].details_stale).toBe(true);
			expect((await loadVendorAccounts()).ok).toBe(true);
			expect(get(vendorAccounts)).toEqual([{ ...original, revision: 2 }]);
		});
		it(`earlier inventory cannot resurrect confirmed removal and fresh GET removes tombstone for ${provider}`, async () => {
			const original = account(provider), removed = { ...original, state: 'removed' as const, revision: 2 };
			vendorAccounts.set([original]);
			const held = deferred();
			vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(held.promise).mockResolvedValueOnce(response(removed))
				.mockResolvedValueOnce(response({ detail: 'offline' }, 503)).mockResolvedValueOnce(list([])));
			const earlier = loadVendorAccounts();
			expect((await removeVendorAccount(original)).ok).toBe(true);
			held.resolve(list([original])); expect(await earlier).toMatchObject({ ok: false, superseded: true });
			expect(get(vendorAccounts)).toEqual([removed]);
			expect((await loadVendorAccounts()).ok).toBe(true);
			expect(get(vendorAccounts)).toEqual([]);
		});
	}
});

describe('historical inventory', () => {
	const response = (data: unknown) => new Response(JSON.stringify(data));
	afterEach(() => { vendorAccounts.set([]); vi.unstubAllGlobals(); });

	it('keeps ordinary inventory unfiltered and requests removed accounts only explicitly', async () => {
		const current = { id: 'current', provider: 'codex', name: 'Current', state: 'signed_in' as const, is_default: false, revision: 1, identity: {}, label: { name: 'Current', email: null, text: 'Current' } };
		const removed = { ...current, id: 'removed', state: 'removed' as const };
		const fetch = vi.fn().mockResolvedValueOnce(response({ accounts: [current] })).mockResolvedValueOnce(response({ accounts: [current, removed] }));
		vi.stubGlobal('fetch', fetch);

		expect((await loadVendorAccounts()).ok).toBe(true);
		expect(new URL(fetch.mock.calls[0][0], 'http://test').searchParams.has('include_removed')).toBe(false);
		expect((await loadVendorAccounts(true)).ok).toBe(true);
		expect(new URL(fetch.mock.calls[1][0], 'http://test').searchParams.get('include_removed')).toBe('true');
		expect(get(vendorAccounts)).toEqual([current, removed]);
	});
});

describe('removed accounts and billing writes', () => {
	const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
	const account = (state: 'signed_in' | 'removed') => ({ id: state, provider: 'codex', name: state, state, is_default: false, revision: 1, identity: {}, label: { name: state, email: null, text: state, custom: null } });
	afterEach(() => { vendorAccounts.set([]); vi.unstubAllGlobals(); });

	it('reads removed accounts without changing the shared list', async () => {
		vendorAccounts.set([account('signed_in')]);
		const fetch = vi.fn().mockResolvedValueOnce(response({ accounts: [account('signed_in'), account('removed')] }));
		vi.stubGlobal('fetch', fetch);
		const result = await fetchRemovedVendorAccounts();
		expect(new URL(fetch.mock.calls[0][0], 'http://test').searchParams.get('include_removed')).toBe('true');
		expect(result).toEqual({ ok: true, accounts: [account('removed')] });
		expect(get(vendorAccounts).map((row) => row.id)).toEqual(['signed_in']);
	});

	it('saves a price and day with the account billing revision and replaces the row', async () => {
		const current = { ...account('signed_in'), billing: null, billing_revision: 3 };
		vendorAccounts.set([current]);
		const fetch = vi.fn().mockResolvedValueOnce(response({ ...current, billing: { monthly_price_cents: 2000, charge_day: 14 }, billing_revision: 4 }));
		vi.stubGlobal('fetch', fetch);
		expect(await setVendorAccountBilling(current, { monthly_price_cents: 2000, charge_day: 14 })).toEqual({ ok: true });
		expect(fetch.mock.calls[0][0]).toContain('/api/vendor-accounts/signed_in/billing');
		expect(fetch.mock.calls[0][1].method).toBe('PUT');
		expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ monthly_price_cents: 2000, charge_day: 14, expected_billing_revision: 3 });
		expect(get(vendorAccounts)[0]).toMatchObject({ billing: { monthly_price_cents: 2000, charge_day: 14 }, billing_revision: 4 });
	});

	it('clears billing with a null pair', async () => {
		const current = { ...account('signed_in'), billing: { monthly_price_cents: 2000, charge_day: 14 }, billing_revision: 4 };
		vendorAccounts.set([current]);
		const fetch = vi.fn().mockResolvedValueOnce(response({ ...current, billing: null, billing_revision: 5 }));
		vi.stubGlobal('fetch', fetch);
		expect(await setVendorAccountBilling(current, { monthly_price_cents: null, charge_day: null })).toEqual({ ok: true });
		expect(JSON.parse(fetch.mock.calls[0][1].body)).toMatchObject({ monthly_price_cents: null, charge_day: null });
		expect(get(vendorAccounts)[0].billing).toBeNull();
	});

	it('returns the server text and status of a stale billing save and keeps the row', async () => {
		const current = { ...account('signed_in'), billing: null, billing_revision: 1 };
		vendorAccounts.set([current]);
		vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({ detail: 'This account changed. Reload and try again.', code: 'billing_changed' }, 409)));
		expect(await setVendorAccountBilling(current, { monthly_price_cents: 100, charge_day: 1 })).toMatchObject({ ok: false, status: 409, error: 'This account changed. Reload and try again.' });
		expect(get(vendorAccounts)[0].billing).toBeNull();
	});

	it('records overage for a month against the billing revision', async () => {
		const current = { ...account('signed_in'), billing: null, billing_revision: 2 };
		vendorAccounts.set([current]);
		const fetch = vi.fn().mockResolvedValueOnce(response({ ...current, billing_revision: 3 }));
		vi.stubGlobal('fetch', fetch);
		expect(await recordVendorAccountOverage(current, { amount_cents: 1250, month: '2026-09' })).toEqual({ ok: true });
		expect(fetch.mock.calls[0][0]).toContain('/api/vendor-accounts/signed_in/overage');
		expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ amount_cents: 1250, month: '2026-09', expected_billing_revision: 2 });
		expect(get(vendorAccounts)[0].billing_revision).toBe(3);
	});

	it('reports a rejected overage with the server text', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({ detail: 'Choose a month.' }, 422)));
		expect(await recordVendorAccountOverage(account('signed_in'), { amount_cents: 1, month: '2026-09' })).toMatchObject({ ok: false, status: 422, error: 'Choose a month.' });
	});
});

describe('stale read races', () => {
	const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
	const account = (provider: string) => ({ id: 'work', provider, name: 'Work', state: 'signed_in' as const, is_default: false, revision: 1, identity: {}, label: { name: 'Work', email: null, text: 'Work' } });
	afterEach(() => { vendorAccounts.set([]); vi.unstubAllGlobals(); });

	it.each(['codex', 'claude-code'])('an earlier inventory read cannot undo confirmed sign-out for %s', async (provider) => {
	 const original = account(provider);
	 vendorAccounts.set([original]);
	 let resolveOld!: (reply: Response) => void;
	 const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { resolveOld = resolve; }))
	  .mockResolvedValueOnce(response({ ...original, state: 'signed_out', revision: 2 }))
	  .mockResolvedValueOnce(response({ detail: 'offline' }, 503));
	 vi.stubGlobal('fetch', fetch);
	 const earlierRead = loadVendorAccounts();
	 const mutation = await signOutVendorAccount(original);
	 expect(mutation.ok).toBe(true);
	 expect(get(vendorAccounts)[0]).toMatchObject({ state: 'signed_out', revision: 2 });
	 resolveOld(response({ accounts: [original] }));
	 await earlierRead;
	 expect(get(vendorAccounts)[0]).toMatchObject({ state: 'signed_out', revision: 2 });
	});

	it.each(['codex', 'claude-code'])('an earlier inventory read cannot erase confirmed sign-in while details are stale for %s', async (provider) => {
	 const original = { ...account(provider), state: 'signed_out' as const, revision: 0 };
	 vendorAccounts.set([original]);
	 let resolveOld!: (reply: Response) => void;
	 const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { resolveOld = resolve; }))
	  .mockResolvedValueOnce(response({ account_id: 'work', status: 'completed', route: 'browser', cause: null }))
	  .mockResolvedValueOnce(response({ detail: 'offline' }, 503));
	 vi.stubGlobal('fetch', fetch);
	 const earlierRead = loadVendorAccounts();
	 const completion = await checkVendorSignin('work');
	 expect(completion.ok).toBe(true);
	 expect(get(vendorAccounts)[0]).toMatchObject({ state: 'signed_in', revision: 0, details_stale: true });
	 resolveOld(response({ accounts: [original] }));
	 await earlierRead;
	 expect(get(vendorAccounts)[0]).toMatchObject({ state: 'signed_in', revision: 0, details_stale: true });
	});

	it.each(['codex', 'claude-code'])('an earlier inventory read cannot remove a newly added account for %s', async (provider) => {
	 const ambient = { ...account(provider), id: 'default', name: 'Default', state: 'signed_in' as const, is_default: true, revision: 0 };
	 const added = { ...account(provider), state: 'signed_out' as const, revision: 0 };
	 vendorAccounts.set([ambient]);
	 let resolveOld!: (reply: Response) => void;
	 const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { resolveOld = resolve; }))
	  .mockResolvedValueOnce(response(added));
	 vi.stubGlobal('fetch', fetch);
	 const earlierRead = loadVendorAccounts();
	 expect((await addVendorAccount(provider, 'Work')).ok).toBe(true);
	 expect(get(vendorAccounts).map(row => row.id)).toContain('work');
	 resolveOld(response({ accounts: [ambient] }));
	 await earlierRead;
	 expect(get(vendorAccounts).map(row => row.id)).toContain('work');
	});

	it('a sign-in reload overtaken by a newer read is not reported as a failure', async () => {
	 vendorAccounts.set([{ ...account('codex'), state: 'signed_out' as const }]);
	 let resolveReload!: (reply: Response) => void;
	 const fetch = vi.fn()
	  .mockResolvedValueOnce(response({ account_id: 'work', status: 'cancelled', route: 'browser', cause: null }))
	  .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveReload = resolve; }))
	  .mockResolvedValueOnce(response({ accounts: [] }));
	 vi.stubGlobal('fetch', fetch);
	 const cancel = cancelVendorSignin('work');
	 await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
	 expect((await loadVendorAccounts()).ok).toBe(true);
	 resolveReload(response({ accounts: [account('codex')] }));
	 const result = await cancel;
	 expect(result.error).toBeUndefined();
	 expect(get(vendorAccounts)).toEqual([]);
	});
});
