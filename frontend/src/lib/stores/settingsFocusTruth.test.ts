import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { token } from './auth';
import { coreSettings, loadSettings, updateSettings } from './settings';

const snapshot = {
	workspace_context_files: [],
};

describe('Focus settings truth', () => {
	afterEach(() => {
		vi.restoreAllMocks();
		coreSettings.set(null);
		token.set(null);
	});

	it('turns Focus off when an accepted snapshot omits its server feature', async () => {
		token.set('test-token');
		coreSettings.set({ ...snapshot, features: { focus_mode: true } });
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ...snapshot, features: {} }) })));

		await loadSettings();

		expect(get(coreSettings)?.features?.focus_mode).toBeUndefined();
	});

	it('does not retain Focus from a partial feature PATCH response', async () => {
		token.set('test-token');
		coreSettings.set({ ...snapshot, features: { focus_mode: true, debug_version_overlay: false } });
		vi.stubGlobal('fetch', vi.fn(async () => ({
			ok: true, json: async () => ({ features: { debug_version_overlay: true } }),
		})));

		await expect(updateSettings({ features: { debug_version_overlay: true } })).resolves.toEqual({ ok: true });

		expect(get(coreSettings)?.features?.focus_mode).toBeUndefined();
	});
});
