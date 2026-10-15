import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from './auth';
import { retryLocalSetup } from './localPickerStatus';
import { setupLocalSource } from '$lib/conductorSources';

vi.mock('$lib/conductorSources', () => ({
	fetchSources: vi.fn(async () => []),
	localPickerStatus: vi.fn(() => null),
	setupLocalSource: vi.fn(),
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

	it('returns consent_required for the picker to show explicit remediation', async () => {
		vi.mocked(setupLocalSource).mockResolvedValue({ ok: false, code: 'consent_required' });

		expect(await retryLocalSetup('setup')).toEqual({ ok: false, code: 'consent_required' });
	});
});
