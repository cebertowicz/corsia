import { afterEach, describe, expect, it, vi } from 'vitest';
import { hasTauriInvoke, installTauriShell } from './tauriShell';

afterEach(() => {
	document.documentElement.dataset.shell = '';
	vi.unstubAllGlobals();
});

describe('installTauriShell', () => {
	it('marks the document before consumers install when the Tauri bridge can invoke commands', () => {
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn() });

		const remove = installTauriShell();

		expect(hasTauriInvoke()).toBe(true);
		expect(document.documentElement.dataset.shell).toBe('tauri');
		remove();
	});

	it('does not mistake a marker bridge object for an invokable desktop bridge', () => {
		vi.stubGlobal('__TAURI_INTERNALS__', {});

		const remove = installTauriShell();

		expect(hasTauriInvoke()).toBe(false);
		expect(document.documentElement.dataset.shell).toBe('');
		remove();
	});

	it('leaves a browser document unmarked', () => {
		const remove = installTauriShell();

		expect(document.documentElement.dataset.shell).toBe('');
		remove();
	});
});
