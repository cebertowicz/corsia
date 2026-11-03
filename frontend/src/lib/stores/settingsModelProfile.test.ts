import { afterEach, describe, expect, it, vi } from 'vitest';

import { token } from '$lib/stores/auth';
import { loadModelProfile } from './settings';

describe('loadModelProfile', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
		token.set(null);
	});

	it('sends the session in the Authorization header, which the Devices listener admits', async () => {
		const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ context_window_override: null }) });
		vi.stubGlobal('fetch', fetchMock);
		token.set('session-1');

		await loadModelProfile('model-1');

		const [url, init] = fetchMock.mock.calls[0];
		expect(String(url)).toContain('/api/models/model-1/profile');
		expect(init.headers).toMatchObject({ Authorization: 'Bearer session-1' });
	});
});
