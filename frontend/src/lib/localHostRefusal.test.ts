import { afterEach, expect, it, vi } from 'vitest';
import { fetchLocalModelRecommendations, setupLocalSource, startLocalModelDownload } from './conductorSources';
import { requestLocalStart } from './setupFormSupport';

const message = "Local models don't run on this computer. Connect a provider.";

afterEach(() => vi.unstubAllGlobals());

it('preserves the non-macOS guidance for stale setup and download actions', async () => {
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: { code: 'never_local_host', message } }) })));
	await expect(setupLocalSource()).rejects.toThrow(message);
	await expect(fetchLocalModelRecommendations()).rejects.toThrow(message);
	await expect(startLocalModelDownload('org/model')).rejects.toThrow(message);
	await expect(requestLocalStart()).resolves.toMatchObject({ result: 'failed', fix: message });
	await expect(requestLocalStart()).resolves.toMatchObject({ result: 'failed', fix: message });
});

it('keeps the existing setup error for an ordinary failure', async () => {
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: 'ordinary error' }) })));
	await expect(setupLocalSource()).rejects.toThrow('local setup 409');
});
