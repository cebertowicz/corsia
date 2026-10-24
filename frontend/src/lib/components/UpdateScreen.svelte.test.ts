import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import UpdateScreen from './UpdateScreen.svelte';
import { updateScreenView } from '$lib/update/screenStore';

let component: Record<string, unknown> | null = null;

function showRemote(view: Parameters<typeof updateScreenView.set>[0], name = 'Studio Mac'): void {
	(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name };
	updateScreenView.set(view);
	component = mount(UpdateScreen, { target: document.body }) as Record<string, unknown>;
	flushSync();
}

afterEach(() => {
	component && unmount(component);
	component = null;
	updateScreenView.set(null);
	delete (window as unknown as Record<string, unknown>).__CORSIA_SHELL__;
});

describe('UpdateScreen remote copy', () => {
	it('names the main computer while progress is estimated remotely', () => {
		showRemote({ mode: 'progress', step: 'restarting', label: 'Restarting CORSIA', percent: 80, estimated: true, fromVersion: null, toVersion: null, failure: null, serviceDown: true });
		expect(document.body.textContent).toContain('Progress shows on your main computer, Studio Mac.');
	});

	it('uses the accepted recovery and main-computer copy after a longer remote restart', () => {
		showRemote({ mode: 'longer', step: 'restarting', label: 'Restarting CORSIA', percent: 80, estimated: true, fromVersion: null, toVersion: null, failure: null, serviceDown: true });
		expect(document.body.textContent).toContain('Open CORSIA on Studio Mac.');
		expect(document.body.textContent).toContain('Progress shows on your main computer, Studio Mac.');
	});

	it('uses the main-computer fallback once when the remote name is absent', () => {
		showRemote({ mode: 'longer', step: 'restarting', label: 'Restarting CORSIA', percent: 80, estimated: true, fromVersion: null, toVersion: null, failure: null, serviceDown: true }, '');
		expect(document.body.textContent).toContain('Open CORSIA on your main computer.');
		expect(document.body.textContent).toContain('Progress shows on your main computer.');
		expect(document.body.textContent).not.toContain('your main computer, your main computer');
	});
});
