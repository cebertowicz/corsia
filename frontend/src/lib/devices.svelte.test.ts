import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { token } from '$lib/stores/auth';
import { handleMessage } from '$lib/ws/handlers';
import { createDevices, viewFromApi, type DevicesApi } from './devices.svelte';

// Several waits here ride real timers (a one-second poll, a 200 ms hold); a loaded machine must not fail them.
const waitFor = <T,>(fn: () => T | Promise<T>, options: { timeout?: number } = {}) => vi.waitFor(fn, { timeout: 4000, ...options });

const api = (patch: Partial<DevicesApi> = {}): DevicesApi => ({
	enabled: true, listening: { port: 47901 }, announced_name: 'Studio', local_hostname: 'studio.local',
	lan_addresses: ['192.168.1.20'], code: { state: 'none' }, ...patch,
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('viewFromApi', () => {
	it('reads the off state', () => {
		const view = viewFromApi(api({ enabled: false, announced_name: null, listening: { error: null } }), false);
		expect(view).toMatchObject({ enabled: false, announced: false, pairing: 'idle', listener: 'ok', readOnly: false });
	});
	it('reads on and visible, with the announced name', () => {
		expect(viewFromApi(api(), false)).toMatchObject({ enabled: true, announced: true, hostName: 'Studio', pairing: 'idle' });
	});
	it('reads on but not announced, and lists what to type with the port when it is not the default', () => {
		const view = viewFromApi(api({ announced_name: null, listening: { port: 47902 }, lan_addresses: ['192.168.1.20', 'fd5c::1'] }), false);
		expect(view).toMatchObject({ announced: false, hostName: 'studio' });
		expect(view.addresses).toEqual(['studio.local:47902', '192.168.1.20:47902', '[fd5c::1]:47902']);
	});
	it('reads an active code with its seconds left', () => {
		expect(viewFromApi(api({ code: { state: 'active', value: 'K7QX', expires_in_seconds: 540 } }), false)).toMatchObject({ pairing: 'code', code: 'K7QX', endsInSeconds: 540 });
	});
	it('reads paused', () => {
		expect(viewFromApi(api({ code: { state: 'paused' } }), false)).toMatchObject({ pairing: 'paused', code: null });
	});
	it('reads all ports taken: off, with a listener error', () => {
		expect(viewFromApi(api({ enabled: false, listening: { error: 'No approved Devices port is available' }, announced_name: null }), false).listener).toBe('all-ports-taken');
	});
	it('is read-only when told so, and when an active code arrives without its characters (G11)', () => {
		expect(viewFromApi(api(), true).readOnly).toBe(true);
		const seen = viewFromApi(api({ code: { state: 'active', expires_in_seconds: 300 } }), false);
		expect(seen).toMatchObject({ readOnly: true, pairing: 'idle', code: null });
	});
	it('uses the connected computer’s name over the one the server reports', () => {
		expect(viewFromApi(api(), true, 'Steve’s Mac Studio').hostName).toBe('Steve’s Mac Studio');
	});
});

describe('createDevices', () => {
	let calls: { url: string; method: string; body?: string; auth?: string }[];
	let responses: Array<Response | (() => Response)>;
	let clock: number;

	beforeEach(() => {
		token.set('t-1');
		calls = []; responses = []; clock = 1_000_000;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			calls.push({ url: String(input), method: init?.method ?? 'GET', body: init?.body as string | undefined, auth: (init?.headers as Record<string, string> | undefined)?.Authorization });
			const next = responses.shift();
			return typeof next === 'function' ? next() : next ?? json(api());
		}));
	});
	afterEach(() => { vi.unstubAllGlobals(); token.set(null); });

	it('reads the host on first load and sends the person’s token', async () => {
		const devices = createDevices({ now: () => clock });
		responses.push(json(api()));
		expect(await devices.read()).toBe(true);
		expect(devices.view).toMatchObject({ load: 'ready', enabled: true });
		expect(calls[0]).toMatchObject({ url: '/api/devices', method: 'GET', auth: 'Bearer t-1' });
	});

	it('shows Couldn’t load when the first read fails, and Try again reads again', async () => {
		const devices = createDevices({ now: () => clock });
		responses.push(json({}, 500));
		await devices.read();
		expect(devices.view.load).toBe('failed');
		responses.push(json(api({ enabled: false, announced_name: null })));
		await devices.retry();
		expect(devices.view).toMatchObject({ load: 'ready', enabled: false });
	});

	it('turns on with PUT and shows the first code at once', async () => {
		const devices = createDevices({ now: () => clock });
		responses.push(json(api({ enabled: false, announced_name: null })));
		await devices.read();
		responses.push(json(api({ code: { state: 'active', value: 'K7QX', expires_in_seconds: 600 } })));
		const turning = devices.setEnabled(true);
		expect(devices.view).toMatchObject({ enabled: true, busy: 'starting' });
		await turning;
		expect(calls[1]).toMatchObject({ url: '/api/devices', method: 'PUT', body: JSON.stringify({ enabled: true }) });
		expect(devices.view).toMatchObject({ busy: null, pairing: 'code', code: 'K7QX', endsInSeconds: 600 });
	});

	it('goes back off with the ports line when every port is taken', async () => {
		const devices = createDevices({ now: () => clock });
		await devices.read();
		responses.push(json(api({ enabled: false, announced_name: null, listening: { error: 'No approved Devices port is available' } })));
		await devices.setEnabled(true);
		expect(devices.view).toMatchObject({ enabled: false, listener: 'all-ports-taken' });
	});

	it('asks for a new code with POST', async () => {
		const devices = createDevices({ now: () => clock });
		await devices.read();
		responses.push(json({ code: 'W4BC', ...api({ code: { state: 'active', value: 'W4BC', expires_in_seconds: 600 } }) }));
		await devices.newCode();
		expect(calls.at(-1)).toMatchObject({ url: '/api/devices/code', method: 'POST' });
		expect(devices.view).toMatchObject({ pairing: 'code', code: 'W4BC' });
	});

	it('turns read-only when the host refuses a change from a connected computer', async () => {
		const devices = createDevices({ now: () => clock });
		await devices.read();
		responses.push(json({ detail: 'Manage connections on Studio itself' }, 403));
		responses.push(json(api()));
		await devices.setEnabled(false);
		expect(devices.view.readOnly).toBe(true);
	});

	it('counts the code down from the seconds received, to 0, where the row reads the host again', async () => {
		const devices = createDevices({ now: () => clock });
		responses.push(json(api({ code: { state: 'active', value: 'K7QX', expires_in_seconds: 90 } })));
		await devices.read();
		clock += 31_000; devices.tick();
		expect(devices.view.endsInSeconds).toBe(59);
		clock += 60_000; devices.tick();
		expect(devices.view.endsInSeconds).toBe(0);
		expect(calls).toHaveLength(1);
	});

	it('re-reads on the host’s devices_updated, so a pairing that used the code returns the row to New code', async () => {
		const devices = createDevices({ now: () => clock });
		responses.push(json(api({ code: { state: 'active', value: 'K7QX', expires_in_seconds: 540 } })));
		const stop = devices.start();
		await waitFor(() => expect(devices.view.pairing).toBe('code'));
		responses.push(json(api({ code: { state: 'none' } })));
		handleMessage({ type: 'devices_updated' }, vi.fn());
		await waitFor(() => expect(devices.view).toMatchObject({ pairing: 'idle', code: null, endsInSeconds: null }));
		expect(calls).toHaveLength(2);
		stop();
		handleMessage({ type: 'devices_updated' }, vi.fn());
		expect(calls).toHaveLength(2);
	});

	it('does not re-read over a start in progress', async () => {
		const devices = createDevices({ now: () => clock });
		responses.push(json(api({ enabled: false, announced_name: null })));
		await devices.read();
		const stop = devices.start();
		await waitFor(() => expect(calls).toHaveLength(2));
		responses.push(() => new Response(null, { status: 200 }));
		void devices.setEnabled(true);
		const before = calls.length;
		handleMessage({ type: 'devices_updated' }, vi.fn());
		expect(calls).toHaveLength(before);
		stop();
	});

	it('takes the connected computer’s name from the shell', async () => {
		const devices = createDevices({ now: () => clock, remoteName: () => 'Steve’s Mac Studio' });
		await devices.read();
		expect(devices.view).toMatchObject({ readOnly: true, hostName: 'Steve’s Mac Studio' });
	});
});
