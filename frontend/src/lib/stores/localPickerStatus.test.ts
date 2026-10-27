import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from './auth';
import { get } from 'svelte/store';
import { canAddLocalModel, canSetUpLocalModel, neverLocalHost, retryLocalSetup, watchLocalStatus } from './localPickerStatus';
import { fetchSources, setupLocalSource, startLocalModelDownload } from '$lib/conductorSources';

vi.mock('$lib/conductorSources', () => ({
	fetchSources: vi.fn(async () => []),
	localPickerStatus: vi.fn(() => null),
	setupLocalSource: vi.fn(),
	startLocalModelDownload: vi.fn(),
}));

vi.mock('$lib/conductorSourcesApi', () => ({
	addStarterModel: vi.fn(),
}));

vi.mock('$lib/ws/events', () => ({ on: vi.fn(() => () => {}) }));

describe('retryLocalSetup', () => {
	beforeEach(() => token.set('token'));
	afterEach(() => {
		token.set(null);
		vi.clearAllMocks();
	});

	it('retries setup without asking about other oMLX processes', async () => {
		vi.mocked(setupLocalSource).mockResolvedValue({ ok: true });

		expect(await retryLocalSetup('setup')).toEqual({ ok: true });
		expect(setupLocalSource).toHaveBeenCalledWith('token');
	});

	it('retries a Settings download through conductorSources after the models_updated read', async () => {
		vi.mocked(fetchSources).mockResolvedValue([{ source: 'omlx', name: 'Local model', state: 'ready', settings_download: { name: 'Qwen', repository: 'org/model', percent: null, failed: true } }]);
		const stop = watchLocalStatus();
		await vi.waitFor(() => expect(fetchSources).toHaveBeenCalledWith('token'));

		expect(await retryLocalSetup('settings')).toEqual({ ok: true });
		expect(startLocalModelDownload).toHaveBeenCalledWith('token', 'org/model');
		stop();
	});

	it('keeps the backend refusal when a Settings retry is refused', async () => {
		vi.mocked(fetchSources).mockResolvedValue([{ source: 'omlx', name: 'Local model', state: 'ready', settings_download: { name: 'Qwen', repository: 'org/model', percent: null, failed: true } }]);
		vi.mocked(startLocalModelDownload).mockRejectedValue(new Error("Couldn't download that model: it isn't in the list for this computer right now. Pick another from the list."));
		const stop = watchLocalStatus();
		await vi.waitFor(() => expect(fetchSources).toHaveBeenCalledWith('token'));

		expect(await retryLocalSetup('settings')).toEqual({ ok: false, message: "Couldn't download that model: it isn't in the list for this computer right now. Pick another from the list." });
		stop();
	});
});

describe('what the local row lets the picker offer', () => {
	beforeEach(() => token.set('token'));
	afterEach(() => {
		token.set(null);
		vi.clearAllMocks();
	});

	async function read(row: Record<string, unknown>) {
		vi.mocked(fetchSources).mockResolvedValue([{ source: 'omlx', name: 'Local model', state: 'needs_setup', ...row }] as never);
		const stop = watchLocalStatus();
		await vi.waitFor(() => expect(fetchSources).toHaveBeenCalledWith('token'));
		await new Promise((resolve) => setTimeout(resolve, 0)); // the read resolves into the store
		return stop;
	}

	it.each([
		['an eligible Mac without its own local service', { eligible: true }, { setUp: true, add: false, never: false }],
		['an eligible Mac with its own local service online', { eligible: true, corsia_omlx: true, corsia_omlx_online: true }, { setUp: false, add: true, never: false }],
		['a Mac too small for a local model', { eligible: false }, { setUp: false, add: false, never: false }],
		['a server that is not macOS with enough memory', { eligible: false, never_local: true }, { setUp: false, add: false, never: true }],
		['a server that is not macOS with its own service listed', { eligible: false, never_local: true, corsia_omlx: true, corsia_omlx_online: true }, { setUp: false, add: false, never: true }],
	])('%s', async (_name, row, expected) => {
		const stop = await read(row);
		expect({ setUp: get(canSetUpLocalModel), add: get(canAddLocalModel), never: get(neverLocalHost) }).toEqual(expected);
		stop();
	});
});
