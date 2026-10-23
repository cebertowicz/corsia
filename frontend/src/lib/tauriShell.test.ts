import { afterEach, describe, expect, it, vi } from 'vitest';
import { installTauriShell } from './tauriShell';

afterEach(() => {
	document.documentElement.dataset.shell = '';
	vi.unstubAllGlobals();
});

describe('installTauriShell', () => {
	it('marks the document before consumers install when the Tauri bridge is available', () => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn() });

		const remove = installTauriShell();

		expect(document.documentElement.dataset.shell).toBe('tauri');
		remove();
	});

	it('leaves a browser document unmarked', () => {
		const remove = installTauriShell();

		expect(document.documentElement.dataset.shell).toBe('');
		remove();
	});
});
