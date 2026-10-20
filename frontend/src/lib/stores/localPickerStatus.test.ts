import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from './auth';
import { retryLocalSetup, watchLocalStatus } from './localPickerStatus';
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
