import { afterEach, expect, it, vi } from 'vitest';
import { fetchLocalModelRecommendations, setupLocalSource, startLocalModelDownload } from './conductorSources';
import { requestLocalStart } from './setupFormSupport';

const message = "Local models don't run on this computer. Connect a provider.";

afterEach(() => vi.unstubAllGlobals());

it('preserves the non-macOS guidance for stale setup and download actions', async () => {
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: { code: 'never_local_host', message } }) })));
	await expect(setupLocalSource('token')).rejects.toThrow(message);
	await expect(fetchLocalModelRecommendations('token')).rejects.toThrow(message);
	await expect(startLocalModelDownload('token', 'org/model')).rejects.toThrow(message);
	await expect(requestLocalStart(null)).resolves.toMatchObject({ result: 'failed', fix: message });
	await expect(requestLocalStart('token')).resolves.toMatchObject({ result: 'failed', fix: message });
});

it('keeps the existing setup error for an ordinary failure', async () => {
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: 'ordinary error' }) })));
	await expect(setupLocalSource('token')).rejects.toThrow('local setup 409');
});
