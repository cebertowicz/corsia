import { afterEach, describe, expect, it, vi } from 'vitest';

import { requestLocalDownload } from './setupLocalDownload';

afterEach(() => vi.unstubAllGlobals());

describe('requestLocalDownload', () => {
	it.each([
		['fresh install', false, false, { consent_existing: false }],
		['reuse consent', true, false, { consent_existing: true }],
		['replacement consent', false, true, { consent_existing: false, consent_replace: true }],
	])('sends the %s request body', async (_name, consentExisting, consentReplace, body) => {
		const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
		vi.stubGlobal('fetch', fetchMock);

		expect(await requestLocalDownload(true, consentExisting, consentReplace)).toEqual({
			outcome: 'started',
			stage: null,
		});
		expect(fetchMock).toHaveBeenCalledWith('/api/setup/local-download', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		});
	});
});
