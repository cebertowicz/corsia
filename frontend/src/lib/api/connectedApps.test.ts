import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	loadConnectedApps,
	loadConnectedAppsRows,
	needsManualGoogleStep,
	startAppSignIn,
	removeAccount,
	saveAccountReach,
	signOutOfAccount,
	startAccountSignIn,
	type ConnectedAccount,
	type ConnectedAppsRow,
	type RevocationCause,
} from './connectedApps';

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

const account: ConnectedAccount = {
	kind: 'account', id: 'acc 1', provider: 'google', address: 'person@example.com',
	capabilities: ['mail', 'calendar', 'files'], state: 'connected',
	reach: { mode: 'selected', project_ids: [], outside_projects: false },
};
const app = { id: 'app-1', name: 'GitHub', state: 'working', revision: 3, reach: { mode: 'all' as const, project_ids: [], outside_projects: false } };

function answer(body: unknown) {
	const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue(body) });
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
}

function sent(fetchMock: ReturnType<typeof vi.fn>) {
	const [path, init] = fetchMock.mock.calls[0];
	return { path: String(path).split('?')[0], init, body: init?.body ? JSON.parse(init.body) : undefined };
}

describe('the Connected apps rows', () => {
	it('puts each Google account beside the apps as its own tagged row', async () => {
		answer({ servers: [app], accounts: [account, { ...account, id: 'acc-2', address: 'work@example.com' }], sign_in_here: true });

		const { rows, signInHere } = await loadConnectedAppsRows();

		expect(signInHere).toBe(true);
		expect(rows.map((row) => [row.kind, row.id])).toEqual([['account', 'acc 1'], ['account', 'acc-2'], ['hosted_app', 'app-1']]);
		const first: ConnectedAppsRow = rows[0];
		if (first.kind === 'account') expect(first.address).toBe('person@example.com');
		else throw new Error('expected an account row');
	});

	it('still lists only apps for the existing callers, and copes with a server that sends no accounts', async () => {
		answer({ servers: [app], accounts: [account] });
		expect(await loadConnectedApps()).toEqual([app]);
		answer({ servers: [app] });
		expect((await loadConnectedAppsRows()).rows.map((row) => row.kind)).toEqual(['hosted_app']);
	});
});

describe('the account controls', () => {
	it('signs an account in again by naming it, and gets the link to open', async () => {
		const fetchMock = answer({ url: 'https://accounts.example/auth', account_id: 'acc 1' });

		await expect(startAccountSignIn('acc 1')).resolves.toEqual({ url: 'https://accounts.example/auth', account_id: 'acc 1' });

		const call = sent(fetchMock);
		expect(call.path).toBe('/api/google/connect');
		expect(call.init.method).toBe('POST');
		expect(call.body).toEqual({ account_id: 'acc 1' });
	});

	it('uses the approved Google fallback when an account sign-in cannot start', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => null }));
		await expect(startAccountSignIn('acc 1')).rejects.toThrow("Couldn't start Google sign-in. Try again.");
	});

	it('uses the neighbouring generic fallback when an app sign-in cannot start', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, text: async () => '', clone() { return this; }, json: async () => null }));
		await expect(startAppSignIn('app-1')).rejects.toThrow("Couldn't start the sign-in. Try again.");
	});

	it('signs one account out and returns its updated row and the revocation cause', async () => {
		const fetchMock = answer({ row: { ...account, state: 'signed_out' }, revocation: 'revoked' });

		const result = await signOutOfAccount('acc 1');

		expect(result.row.state).toBe('signed_out');
		expect(result.revocation).toBe('revoked');
		expect(sent(fetchMock)).toMatchObject({ path: '/api/mcp/accounts/acc%201/sign-out', init: { method: 'POST' } });
	});

	it('removes one account and returns the cause', async () => {
		const fetchMock = answer({ id: 'acc 1', removed: true, revocation: 'not_signed_in' });

		await expect(removeAccount('acc 1')).resolves.toEqual({ id: 'acc 1', removed: true, revocation: 'not_signed_in' });

		expect(sent(fetchMock)).toMatchObject({ path: '/api/mcp/accounts/acc%201', init: { method: 'DELETE' } });
	});

	it('saves where it works with the reach alone, and returns the row', async () => {
		const reach = { mode: 'selected' as const, project_ids: ['p1'], outside_projects: false };
		const fetchMock = answer({ row: { ...account, reach } });

		await expect(saveAccountReach('acc 1', reach)).resolves.toEqual({ row: { ...account, reach } });

		const call = sent(fetchMock);
		expect(call.path).toBe('/api/mcp/accounts/acc%201/reach');
		expect(call.init.method).toBe('PUT');
		expect(call.body).toEqual({ reach });
	});

	it('shows the fixed refusal the server sends, and its own plain sentence for a code', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const refuse = (detail: string) => vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({ detail }) }));
		refuse('Only the signed-in person can widen where an account works; open Connected apps.');
		await expect(saveAccountReach('acc 1', account.reach)).rejects.toThrow('Only the signed-in person can widen where an account works; open Connected apps.');
		refuse('connected_account_gone');
		await expect(signOutOfAccount('acc 1')).rejects.toThrow("Couldn't change this Google account. Try again.");
	});
});

describe('which revocation causes show the manual Google Account step', () => {
	it('shows it only when Google could not be asked or refused', () => {
		const shown: RevocationCause[] = ['provider_unreachable', 'provider_timeout', 'provider_rejected', 'provider_unexpected'];
		const quiet: RevocationCause[] = ['revoked', 'not_signed_in'];
		expect(shown.map(needsManualGoogleStep)).toEqual([true, true, true, true]);
		expect(quiet.map(needsManualGoogleStep)).toEqual([false, false]);
	});
});
