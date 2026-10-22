import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from '$lib/stores/auth';
import { saveCloudModelsOff } from './providerPolicy';

const response = (status: number, body: unknown) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

describe('saveCloudModelsOff failure text', () => {
	beforeEach(() => token.set('tok'));
	afterEach(() => { vi.unstubAllGlobals(); token.set(null); });

	it('passes on a sentence the server wrote', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(409, { detail: 'Project not found.' })));
		expect(await saveCloudModelsOff('p1', true)).toMatchObject({ ok: false, detail: 'Project not found.', reached: true });
	});

	it('passes a code through as detail and marks an unreachable server', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => response(403, { detail: 'Only the signed-in person can change where models run. Change it in CORSIA while signed in.' })));
		expect(await saveCloudModelsOff('p1', true)).toMatchObject({ ok: false, detail: 'Only the signed-in person can change where models run. Change it in CORSIA while signed in.', reached: true });
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
		expect(await saveCloudModelsOff('p1', false)).toMatchObject({ ok: false, reached: false });
	});
});
