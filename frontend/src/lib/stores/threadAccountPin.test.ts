import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const reloadAccounts = vi.hoisted(() => vi.fn(async () => {}));
const reloadModels = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('./threadVendorAccounts', async (importOriginal) => ({
	...(await importOriginal<typeof import('./threadVendorAccounts')>()),
	loadThreadVendorAccounts: reloadAccounts,
}));
vi.mock('./modelsLoad', () => ({ loadModels: reloadModels }));

import { PIN_FAILED, pinThreadAccount } from './threadAccountPin';
import { token } from './auth';
import { threads } from './threads';
import { SERVER_UNREACHABLE } from '../plainError';

const THREAD = 'thread-1';
const fetchMock = vi.fn();

function reply(status: number, body: unknown) {
	fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}

beforeEach(() => {
	token.set('synthetic');
	threads.set([{ id: THREAD, model: 'claude-code/opus', updated_at: '2026-10-05T10:00:00Z', account_id: 'work', runtime_context_epoch: 4 } as never]);
	fetchMock.mockReset();
	reloadAccounts.mockClear();
	reloadModels.mockClear();
	vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
	vi.unstubAllGlobals();
	token.set(null);
	threads.set([]);
});

describe('pinThreadAccount', () => {
	it('sends one PUT carrying only the account', async () => {
		reply(200, { account_id: 'personal', runtime_context_epoch: 5, changed: true });
		const result = await pinThreadAccount(THREAD, 'personal');
		expect(result).toEqual({ ok: true, changed: true });
		expect(fetchMock).toHaveBeenCalledTimes(1);
		const [url, init] = fetchMock.mock.calls[0];
		expect(String(url)).toMatch(new RegExp(`^/api/threads/${THREAD}/vendor-account\\?token=`));
		expect(init.method).toBe('PUT');
		expect(JSON.parse(init.body)).toEqual({ account_id: 'personal' });
	});

	it('follows the response: the thread binding, then the account read and the catalog', async () => {
		reply(200, { account_id: 'personal', runtime_context_epoch: 5, changed: true });
		await pinThreadAccount(THREAD, 'personal');
		expect(get(threads)[0]).toMatchObject({ account_id: 'personal', runtime_context_epoch: 5 });
		expect(reloadAccounts).toHaveBeenCalledWith(THREAD, 'epoch-5');
		expect(reloadModels).toHaveBeenCalledTimes(1);
	});

	it('accepts a choice held for the next turn: the binding stays, the catalog marks the choice', async () => {
		reply(200, { account_id: 'personal', runtime_context_epoch: 4, changed: false, pending: true });
		expect(await pinThreadAccount(THREAD, 'personal')).toEqual({ ok: true, changed: false });
		expect(get(threads)[0]).toMatchObject({ account_id: 'work', runtime_context_epoch: 4 });
		expect(reloadModels).toHaveBeenCalledTimes(1);
		expect(reloadAccounts).not.toHaveBeenCalled();
	});

	it('says a running turn is the reason, in the one sentence the picker shows', async () => {
		reply(409, { detail: 'account_change_requires_idle; wait for the turn to finish' });
		const result = await pinThreadAccount(THREAD, 'personal');
		expect(result).toEqual({ ok: false, message: 'A turn is running; try again when it finishes.' });
		expect(get(threads)[0].account_id).toBe('work');
		expect(reloadModels).not.toHaveBeenCalled();
	});

	it('pins a thread whose epoch is unknown or older than the server\'s, and follows the server\'s newer one', async () => {
		threads.set([{ id: THREAD, model: 'm', updated_at: '2026-10-05T10:00:00Z' } as never]);
		reply(200, { account_id: 'personal', runtime_context_epoch: 9, changed: true });
		expect(await pinThreadAccount(THREAD, 'personal')).toEqual({ ok: true, changed: true });
		expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ account_id: 'personal' });
		expect(get(threads)[0]).toMatchObject({ account_id: 'personal', runtime_context_epoch: 9 });
	});

	it('gives every other refusal the generic sentence, never the raw reason', async () => {
		reply(422, { detail: 'account_not_permitted; nope' });
		const result = await pinThreadAccount(THREAD, 'personal');
		expect(result).toEqual({ ok: false, message: PIN_FAILED });
		expect(JSON.stringify(result)).not.toContain('account_not_permitted');
		reply(500, null);
		expect(await pinThreadAccount(THREAD, 'personal')).toEqual({ ok: false, message: PIN_FAILED });
	});

	it('reports an unreachable server without moving anything', async () => {
		fetchMock.mockRejectedValueOnce(new TypeError('offline'));
		expect(await pinThreadAccount(THREAD, 'personal')).toEqual({ ok: false, message: SERVER_UNREACHABLE });
		expect(get(threads)[0].account_id).toBe('work');
	});

	it('sends nothing without a sign-in', async () => {
		token.set(null);
		expect(await pinThreadAccount(THREAD, 'personal')).toEqual({ ok: false, message: PIN_FAILED });
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('refuses a response that does not carry the new binding', async () => {
		reply(200, { changed: true });
		expect(await pinThreadAccount(THREAD, 'personal')).toEqual({ ok: false, message: PIN_FAILED });
		expect(get(threads)[0].account_id).toBe('work');
	});
});
