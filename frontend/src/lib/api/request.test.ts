import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { token as session } from '$lib/stores/sessionToken';
import { apiFetch } from './request';

const fetchMock = vi.fn(async () => new Response('{}'));

beforeEach(() => {
	vi.stubGlobal('fetch', fetchMock);
	session.set('signed-in');
});

afterEach(() => {
	fetchMock.mockClear();
	vi.unstubAllGlobals();
	session.set(null);
});

type Sent = [string, { headers: Record<string, string> | Headers }];
const sent = (): Sent => fetchMock.mock.calls[0] as unknown as Sent;
const authorization = (headers: Record<string, string> | Headers): string | null =>
	headers instanceof Headers ? headers.get('Authorization') : headers.Authorization ?? null;

describe('apiFetch owns the session of a request', () => {
	it('signs a call in the header and leaves the address alone by default', async () => {
		await apiFetch('/api/things');
		const [url, init] = sent();
		expect(url).toBe('/api/things');
		expect(authorization(init.headers)).toBe('Bearer signed-in');
	});

	it('puts the same session in the address when the route needs it there', async () => {
		await apiFetch('/api/things?limit=5', { sessionInAddress: true });
		const [url, init] = sent();
		expect(url).toBe('/api/things?token=signed-in&limit=5');
		expect(authorization(init.headers)).toBe('Bearer signed-in');
	});

	it('follows the signed-in session when it changes, in the header and the address together', async () => {
		session.set('second one');
		await apiFetch('/api/things', { sessionInAddress: true });
		const [url, init] = sent();
		expect(url).toBe('/api/things?token=second%20one');
		expect(authorization(init.headers)).toBe('Bearer second one');
	});

	it('adds the address form to a bare question mark without a dangling separator', async () => {
		await apiFetch('/api/things?', { sessionInAddress: true });
		expect(sent()[0]).toBe('/api/things?token=signed-in');
	});

	it('keeps a caller\'s own headers and the rest of its options', async () => {
		await apiFetch('/api/things', { method: 'POST', sessionInAddress: true, headers: { 'Content-Type': 'application/json' }, body: '{}' });
		const [, init] = sent();
		expect(init).toMatchObject({ method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer signed-in' } });
		expect(init).not.toHaveProperty('sessionInAddress');
	});

	it('signs a call whose headers arrive as a Headers object', async () => {
		await apiFetch('/api/things', { headers: new Headers({ 'If-None-Match': 'v1' }) });
		const [, init] = sent();
		expect(init.headers).toBeInstanceOf(Headers);
		expect(authorization(init.headers)).toBe('Bearer signed-in');
		expect((init.headers as Headers).get('If-None-Match')).toBe('v1');
	});

	it('sends a signed-out call unsigned, address form included', async () => {
		session.set(null);
		await apiFetch('/api/auth/login', { method: 'POST', sessionInAddress: true });
		const [url, init] = fetchMock.mock.calls[0] as unknown as [string, Record<string, unknown>];
		expect(url).toBe('/api/auth/login');
		expect(init).toEqual({ method: 'POST' });
	});
});
