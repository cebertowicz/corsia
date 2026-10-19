import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from '$lib/stores/auth';
import { SERVER_UNREACHABLE } from '$lib/plainError';
import { saveCloudModelsOff } from './providerPolicy';

const response = (status: number, body: unknown) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

describe('saveCloudModelsOff failure text', () => {
	beforeEach(() => token.set('tok'));
	afterEach(() => { vi.unstubAllGlobals(); token.set(null); });

	it('passes on a sentence the server wrote', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(409, { detail: 'Project not found.' })));
		expect(await saveCloudModelsOff('p1', true)).toMatchObject({ ok: false, error: 'Project not found.' });
	});

	it('reads a code or empty body as its own sentence, not the subscriptions one', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(403, { detail: 'person_session_required' })));
		expect(await saveCloudModelsOff('p1', true)).toMatchObject({ ok: false, error: 'The server did not accept the change. Try again.' });
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
		expect(await saveCloudModelsOff('p1', false)).toMatchObject({ ok: false, error: SERVER_UNREACHABLE });
	});
});
