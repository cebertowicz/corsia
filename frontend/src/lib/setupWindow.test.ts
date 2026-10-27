import { afterEach, describe, expect, it, vi } from 'vitest';

import { finishSetupWindow } from './setupWindow';

type Shell = { __TAURI_INTERNALS__?: { invoke?: (command: string) => Promise<unknown> } };

describe('finishSetupWindow', () => {
	afterEach(() => { delete (window as Window & Shell).__TAURI_INTERNALS__; });

	it('asks the desktop shell to grow the window', async () => {
		const invoke = vi.fn(async () => undefined);
		(window as Window & Shell).__TAURI_INTERNALS__ = { invoke };
		await finishSetupWindow();
		expect(invoke).toHaveBeenCalledExactlyOnceWith('finish_setup_window');
	});

	it('does nothing in a browser', async () => {
		await expect(finishSetupWindow()).resolves.toBeUndefined();
	});

	it('keeps going when the shell refuses', async () => {
		(window as Window & Shell).__TAURI_INTERNALS__ = { invoke: async () => { throw new Error('no'); } };
		await expect(finishSetupWindow()).resolves.toBeUndefined();
	});
});
