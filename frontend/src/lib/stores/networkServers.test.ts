import { afterEach, describe, expect, it, vi } from 'vitest';

import { get } from 'svelte/store';
import { token } from './auth';
import {
	addNetworkServer, networkServers, parseModelIds, removeNetworkServer, setNetworkServerBilling, setNetworkServerEnabled, setNetworkServerPrivate, testNetworkServer,
	updateNetworkServer, type NetworkServer,
} from './networkServers';

/**
 * A rejected add is only useful if the reason the backend named survives the
 * round trip. The probe path exists to distinguish a typo from a bad key, a
 * wrong path and an unreachable host; that distinction dies if the form falls
 * back to its own generic sentence, which is what FastAPI's `detail`-shaped
 * error body caused.
 */

const response = (status: number, body: unknown) => ({
	ok: status >= 200 && status < 300,
	status,
	json: async () => body,
});

describe('addNetworkServer failure reporting', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('carries the named transport failure through to the form', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(400, {
			detail: 'Could not reach https://ws.example/compatible-mode/v1/models — ConnectError: refused',
		})));

		const r = await addNetworkServer('Prod', '', 0, {
			base_url: 'https://ws.example/compatible-mode/v1', dialect: 'openai',
		});

		expect(r.ok).toBeFalsy();
		expect(r.message).toContain('ConnectError');
	});

	it('keeps the URL and status when the endpoint answered wrongly', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(400, {
			detail: 'https://api.anthropic.com/v1/models returned HTTP 401',
		})));

		const r = await addNetworkServer('Anthropic', '', 0, {
			base_url: 'https://api.anthropic.com', dialect: 'anthropic',
		});

		expect(r.message).toBe('https://api.anthropic.com/v1/models returned HTTP 401');
	});

	it('still reports a browser-level failure as a failure', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));

		const r = await addNetworkServer('Prod', '10.0.0.5', 8000);

		expect(r.ok).toBeFalsy();
		expect(r.message).toContain('offline');
	});
});

describe('updateNetworkServer', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
		token.set(null);
	});

	// The edit is only reachable if it carries the signed-in session like every
	// other network-server call; a hardcoded query value reads as an expired session.
	it('sends the signed-in session with the PUT', async () => {
		const fetchMock = vi.fn(async () => response(400, { detail: 'stop here' }));
		vi.stubGlobal('fetch', fetchMock);
		token.set('session-under-test');

		await updateNetworkServer('srv-1', { base_url: 'https://ws.example/v1', dialect: 'openai' });

		const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
		expect(url).toBe('/api/settings/network-servers/srv-1?token=session-under-test');
		expect(init.method).toBe('PUT');
	});
});

describe('parseModelIds', () => {
	it('accepts one per line or comma-separated, trimmed and de-duplicated', () => {
		expect(parseModelIds(' glm-5.3,\ndeepseek-v4.1-flash , glm-5.3\n\n')).toEqual([
			'glm-5.3', 'deepseek-v4.1-flash',
		]);
		expect(parseModelIds('   ')).toEqual([]);
	});
});

describe('removeNetworkServer', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('reports why a removal was refused', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(404, { detail: 'Server not found' })));

		const r = await removeNetworkServer('ns-gone');

		expect(r).toEqual({ ok: false, message: 'Server not found' });
	});

	it('reports a browser-level failure instead of swallowing it', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));

		const r = await removeNetworkServer('ns-gone');

		expect(r.ok).toBe(false);
		expect(r.message).toContain('offline');
	});
});

describe('setNetworkServerEnabled', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('switches the one endpoint', async () => {
		const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => response(200, { servers: [] }));
		vi.stubGlobal('fetch', fetchMock);

		const r = await setNetworkServerEnabled('ns-tp', false);

		expect(r).toEqual({ ok: true });
		const [url, init] = fetchMock.mock.calls[0];
		expect(url).toContain('/api/settings/network-servers/ns-tp/enabled');
		expect(init?.method).toBe('PUT');
		expect(JSON.parse(String(init?.body))).toEqual({ enabled: false });
	});

	it('reports why a switch was refused', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(404, { detail: 'Server not found' })));

		expect(await setNetworkServerEnabled('ns-gone', true)).toEqual({ ok: false, message: 'Server not found' });
	});
});

describe('setNetworkServerPrivate', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('preserves the server-written reason for the shared setting formatter', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(404, { detail: 'Server not found' })));
		expect(await setNetworkServerPrivate('ns-gone', true)).toEqual({ ok: false, detail: 'Server not found', reached: true });
	});

	it('distinguishes an absent or machine-readable reason from an unreachable request', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(500, {})));
		expect(await setNetworkServerPrivate('ns-1', true)).toEqual({ ok: false, detail: undefined, reached: true });
		vi.stubGlobal('fetch', vi.fn(async () => response(403, { detail: 'person_session_required' })));
		expect(await setNetworkServerPrivate('ns-1', true)).toEqual({ ok: false, detail: 'person_session_required', reached: true });
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
		expect(await setNetworkServerPrivate('ns-1', false)).toEqual({ ok: false, reached: false });
	});
});

describe('testNetworkServer', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('passes on what the endpoint answered, success or not', async () => {
		vi.stubGlobal('fetch', vi.fn(async (url: string) => (String(url).includes('/test')
			? response(200, { ok: false, message: 'tp.example/v1/models returned HTTP 401' })
			: response(200, { servers: [] }))));

		expect(await testNetworkServer('ns-tp')).toEqual({
			ok: false, message: 'tp.example/v1/models returned HTTP 401',
		});
	});

	it('names a refused test', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(404, { detail: 'Server not found' })));

		expect(await testNetworkServer('ns-gone')).toEqual({ ok: false, message: 'Server not found' });
	});
});

describe('setNetworkServerBilling', () => {
	afterEach(() => vi.unstubAllGlobals());
	const server = { id: 'ns-1', billing: null, billing_revision: 2 } as unknown as NetworkServer;

	it('sends the mode, price, day and revision, and merges the returned server into the list', async () => {
		networkServers.set([{ ...server } as NetworkServer]);
		const fetch = vi.fn(async () => response(200, { server_id: 'ns-1', billing: { mode: 'subscription', monthly_price_cents: 3000, charge_day: 1 }, billing_revision: 3, list_priced: true }));
		vi.stubGlobal('fetch', fetch);
		const result = await setNetworkServerBilling(server, { mode: 'subscription', monthly_price_cents: 3000, charge_day: 1 });
		expect(result).toEqual({ ok: true });
		const calls = fetch.mock.calls as unknown as [string, RequestInit][];
		expect(calls[0][0]).toContain('/api/settings/network-servers/ns-1/billing');
		expect(calls[0][1].method).toBe('PUT');
		expect(JSON.parse(calls[0][1].body as string)).toEqual({ mode: 'subscription', monthly_price_cents: 3000, charge_day: 1, expected_billing_revision: 2 });
		expect(get(networkServers)[0]).toMatchObject({ id: 'ns-1', billing_revision: 3, list_priced: true, billing: { mode: 'subscription' } });
	});

	it('returns the server text and status of a stale save', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(409, { detail: 'This endpoint changed. Reload and try again.', code: 'billing_changed' })));
		expect(await setNetworkServerBilling(server, { mode: null, monthly_price_cents: null, charge_day: null }))
			.toEqual({ ok: false, status: 409, code: 'billing_changed', message: 'This endpoint changed. Reload and try again.' });
	});
});
