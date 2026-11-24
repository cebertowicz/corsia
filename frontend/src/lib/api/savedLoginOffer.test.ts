import { afterEach, describe, expect, it, vi } from 'vitest';
import { saveOffer } from './savedLoginOffer';

const answer = (status: number, body: unknown) => vi.stubGlobal('fetch', vi.fn(async () => ({ ok: status >= 200 && status < 300, status, json: async () => body })));

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('saveOffer: whose words the person reads', () => {
	it('reports a save', async () => {
		answer(200, { saved: true });
		expect(await saveOffer('thread')).toEqual({ saved: true });
	});

	it.each([404, 409])('passes the server’s sentence through for a %i', async (status) => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		answer(status, { detail: 'This login can\u2019t be saved from here anymore. Unless it\u2019s already saved, it\u2019s offered again the next time you sign in.' });
		expect(await saveOffer('thread')).toEqual({ saved: false, message: 'This login can\u2019t be saved from here anymore. Unless it\u2019s already saved, it\u2019s offered again the next time you sign in.' });
	});

	it.each([400, 401, 403, 422, 500, 503])('gives no message for a %i, whatever its body says, so the pane says its own line', async (status) => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		answer(status, { detail: 'Text written for an agent or the session.' });
		expect(await saveOffer('thread')).toEqual({ saved: false, message: null });
	});

	it('gives no message for a 404 or 409 whose body has no usable text', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		for (const body of ['gone', {}, { detail: '  ' }, { detail: [{ msg: 'x' }] }]) {
			answer(409, body);
			expect(await saveOffer('thread')).toEqual({ saved: false, message: null });
		}
	});

	it('gives no message when the request throws', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));
		expect(await saveOffer('thread')).toEqual({ saved: false, message: null });
	});
});
