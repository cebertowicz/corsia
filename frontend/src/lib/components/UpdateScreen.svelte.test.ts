import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import UpdateScreen from './UpdateScreen.svelte';
import { updateScreenView } from '$lib/update/screenStore';
import { updateStatus, type UpdateStatus } from '$lib/update/store';

let component: Record<string, unknown> | null = null;

function showRemote(view: Parameters<typeof updateScreenView.set>[0], name = 'Studio Mac'): void {
	(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'remote', name };
	updateScreenView.set(view);
	component = mount(UpdateScreen, { target: document.body }) as Record<string, unknown>;
	flushSync();
}

function showLocal(view: Parameters<typeof updateScreenView.set>[0]): void {
	(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'local' };
	updateScreenView.set(view);
	component = mount(UpdateScreen, { target: document.body }) as Record<string, unknown>;
	flushSync();
}

afterEach(() => {
	component && unmount(component);
	component = null;
	updateScreenView.set(null);
	updateStatus.set(null);
	delete (window as unknown as Record<string, unknown>).__CORSIA_SHELL__;
	delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

describe('UpdateScreen waiting progress', () => {
	it('shows the live count through ready, handles singular and hides it without agents', () => {
		const view = { operationId: 'update-1', mode: 'progress', step: 'waiting', label: 'Waiting for agents to finish', percent: 12, estimated: false, fromVersion: null, toVersion: null, failure: null, serviceDown: false } as const;
		const operation = { operation_id: 'update-1', kind: 'update', status: 'in_flight', phase: 'draining', drain_total: 12, blocking: 9 } as const;
		updateStatus.set({ operation } as UpdateStatus);
		showLocal(view);
		expect(document.body.textContent).toContain('3 of 12 agents paused');
		updateStatus.set({ operation: { ...operation, phase: 'ready', blocking: 0 } } as UpdateStatus);
		flushSync();
		expect(document.body.textContent).toContain('12 of 12 agents paused');
		updateStatus.set({ operation: { ...operation, drain_total: 1, blocking: 0 } } as UpdateStatus);
		flushSync();
		expect(document.body.textContent).toContain('1 of 1 agent paused');
		updateStatus.set({ operation: { ...operation, drain_total: 0 } } as UpdateStatus);
		flushSync();
		expect(document.body.textContent).not.toContain('agents paused');
		expect(document.body.textContent).toContain('Your agents will resume where they left off after the update.');
	});

	it('shows reassurance on completion', () => {
		showLocal({ operationId: 'update-1', mode: 'done', step: 'verifying', label: 'Checking the new version', percent: 100, estimated: false, fromVersion: null, toVersion: null, failure: null, serviceDown: false });
		expect(document.body.textContent).toContain('Your agents will resume where they left off after the update.');
	});
});

describe('UpdateScreen remote copy', () => {
	it('names the main computer while progress is estimated remotely', () => {
		showRemote({ operationId: 'update-1', mode: 'progress', step: 'restarting', label: 'Restarting CORSIA', percent: 80, estimated: true, fromVersion: null, toVersion: null, failure: null, serviceDown: true });
		expect(document.body.textContent).toContain('Progress shows on your main computer, Studio Mac.');
	});

	it('uses main-computer guidance immediately for a remote restart with no estimate', () => {
		showRemote({ operationId: 'update-1', mode: 'progress', step: 'restarting', label: 'Restarting CORSIA', percent: 80, estimated: false, fromVersion: null, toVersion: null, failure: null, serviceDown: true });
		expect(document.body.textContent).toContain('Open CORSIA on Studio Mac.');
		expect(document.body.textContent).toContain('Progress shows on your main computer, Studio Mac.');
		expect(document.body.textContent).not.toContain('About 80.0%');
	});

	it('uses the accepted recovery and main-computer copy after a longer remote restart', () => {
		showRemote({ operationId: 'update-1', mode: 'longer', step: 'restarting', label: 'Restarting CORSIA', percent: 80, estimated: true, fromVersion: null, toVersion: null, failure: null, serviceDown: true });
		expect(document.body.textContent).toContain('Open CORSIA on Studio Mac.');
		expect(document.body.textContent).toContain('Progress shows on your main computer, Studio Mac.');
	});

	it('shows the manual quit-and-reopen escape when local startup progress cannot be read without starting the service', () => {
		const invoke = vi.fn();
		(window as unknown as { __TAURI_INTERNALS__?: { invoke: typeof invoke } }).__TAURI_INTERNALS__ = { invoke };
		showLocal({ operationId: 'update-1', mode: 'progress', step: 'restarting', label: 'Restarting CORSIA', percent: 80, estimated: false, fromVersion: null, toVersion: null, failure: null, serviceDown: true, startupProgressUnavailable: true });
		expect(document.body.textContent).toContain('Couldn’t read startup progress.');
		expect(document.body.textContent).toContain('quit CORSIA with ⌘Q and open it again.');
		expect(document.body.textContent).not.toContain('Start CORSIA');
		expect(invoke).not.toHaveBeenCalled();
	});

	it('uses the main-computer fallback once when the remote name is absent', () => {
		showRemote({ operationId: 'update-1', mode: 'longer', step: 'restarting', label: 'Restarting CORSIA', percent: 80, estimated: true, fromVersion: null, toVersion: null, failure: null, serviceDown: true }, '');
		expect(document.body.textContent).toContain('Open CORSIA on your main computer.');
		expect(document.body.textContent).toContain('Progress shows on your main computer.');
		expect(document.body.textContent).not.toContain('your main computer, your main computer');
	});
});
