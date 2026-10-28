import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import Sidebar from './Sidebar.svelte';
import { activeThreadId, threads } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';
import { threadSettingsOpen } from '$lib/stores/threadSettings';
import { measurementScope } from '$lib/stores/measurement';
import { measurementOrigin } from '$lib/stores/measurementNavigation';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { SERVER_UNREACHABLE } from '$lib/plainError';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(onMeasurement = vi.fn()) {
	component = mount(Sidebar, {
		target: host,
		props: { open: true, onSelectThread: () => {}, onDeleteThread: () => {}, onOpenSettings: () => {}, onMeasurement },
	});
	flushSync();
	return onMeasurement;
}

const link = () => host.querySelector('a[title="Measurement"]') as HTMLAnchorElement;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	window.history.replaceState(null, '', '/');
	measurementScope.set({ project: 'all', agent: null });
	measurementOrigin.set(null);
	uiScope.set(null);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	uiScope.set(null);
	activeThreadId.set(null);
	threads.set([]);
	measurementScope.set({ project: 'all', agent: null });
	token.set(null);
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('Sidebar deactivation', () => {
	function deactivate() {
		token.set('test');
		threads.set([{
			id: 'agent-b', user_id: 'u', profile: 'personal', title: 'Bugs', model: null, created_at: 'now', updated_at: 'now',
			project_id: null, agent_enabled: 1, agent_status: 'active', agent_responsibility: 'Defects',
		}]);
		render();
		const row = [...host.querySelectorAll('.scoped-item')].find((item) => item.textContent?.includes('Bugs'))!;
		row.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 20, clientY: 20 }));
		flushSync();
		const button = [...host.querySelectorAll('button')].find((item) => item.textContent?.trim() === 'Deactivate Agent')!;
		button.click();
	}

	it.each([
		[500, { detail: 'private detail', reference: 'abc123' }, 'Couldn’t deactivate this agent. Try again. Reference: abc123'],
		[401, { detail: 'private detail' }, 'CORSIA couldn’t verify your sign-in. In Settings → System, select Log out, then sign in again.'],
		[403, { detail: 'private detail' }, 'You don’t have access to deactivate this agent. You can continue in another chat.'],
		[404, { detail: 'private detail', reference: 'ref404' }, 'This agent’s chat isn’t available. Refresh the page to update the sidebar. Reference: ref404'],
		[400, { detail: 'private detail' }, 'Couldn’t deactivate this agent. You can continue in another chat.'],
		[500, { detail: 'private detail', reference: 42 }, 'Couldn’t deactivate this agent. Try again.'],
		[500, { detail: 'private detail', reference: '' }, 'Couldn’t deactivate this agent. Try again.'],
	])('shows an actionable error without exposing server detail for status %i and %o', async (status, body, message) => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status, json: async () => body })));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		deactivate();
		await vi.waitFor(() => expect(get(toasts)[0]?.message).toBe(message));
		expect(get(threads).find((thread) => thread.id === 'agent-b')?.agent_enabled).toBe(1);
	});

	it('shows the shared connection message when the request cannot reach the server', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('network'); }));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		deactivate();
		await vi.waitFor(() => expect(get(toasts)[0]?.message).toBe(SERVER_UNREACHABLE));
		expect(get(threads).find((thread) => thread.id === 'agent-b')?.agent_enabled).toBe(1);
	});

	it('demotes the agent after a successful request', async () => {
		const fetchMock = vi.fn(async () => ({ ok: true, json: async () => [] }));
		vi.stubGlobal('fetch', fetchMock);
		deactivate();
		await vi.waitFor(() => expect(get(toasts)[0]?.message).toBe('Agent deactivated'));
		expect(fetchMock).toHaveBeenCalledWith('/api/agents/agent-b?token=test', { method: 'DELETE' });
		expect(get(threads).find((thread) => thread.id === 'agent-b')?.agent_enabled).toBe(0);
	});
});

describe('Sidebar jump tooltip', () => {
	it('describes the searchable projects and chats', () => {
		render();
		expect(host.querySelector('button[title="Jump to a project or chat"]')).not.toBeNull();
	});
});

describe('Sidebar Measurement link', () => {
	it('opens the current project, not the open agent thread, whatever scope was used last', () => {
		threads.set([{
			id: 'agent-b', user_id: 'u', profile: 'personal', title: 'Bugs', model: null, created_at: 'now', updated_at: 'now',
			project_id: 'prj-a', agent_enabled: 1, agent_status: 'active', agent_responsibility: 'Defects',
		}]);
		activeThreadId.set('agent-b');
		uiScope.set({ id: 'prj-a', name: 'Atlas', color: '#d65a87' });
		measurementScope.set({ project: 'prj-z', agent: 'agent-z' });
		const opened = render();

		expect(link().getAttribute('href')).toBe('/?view=measurement&project=prj-a');
		link().click();
		flushSync();

		expect(opened).toHaveBeenCalledTimes(1);
		expect(get(measurementScope)).toEqual({ project: 'prj-a', agent: null });
		expect(get(measurementOrigin)).toBeNull();
	});

	it('opens All work outside any project', () => {
		measurementScope.set({ project: 'prj-z', agent: 'agent-z' });
		render();

		expect(link().getAttribute('href')).toBe('/?view=measurement');
		link().click();
		flushSync();

		expect(get(measurementScope)).toEqual({ project: 'all', agent: null });
	});

	it('leaves a modified or middle click to the browser so the link opens in a new tab', () => {
		measurementScope.set({ project: 'prj-z', agent: 'agent-z' });
		const opened = render();

		for (const init of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { button: 1 }]) {
			const event = new MouseEvent('click', { bubbles: true, cancelable: true, ...init });
			link().dispatchEvent(event);
			expect(event.defaultPrevented).toBe(false);
		}
		expect(opened).not.toHaveBeenCalled();
		expect(get(measurementScope)).toEqual({ project: 'prj-z', agent: 'agent-z' });
	});
});

describe('Sidebar Conductor row menu', () => {
	async function openAgentSettings(onConductor: () => Promise<boolean>) {
		threads.set([{
			id: 'cond', user_id: 'u', profile: 'personal', title: 'Conductor', model: null, created_at: 'now', updated_at: 'now',
			project_id: null, agent_enabled: 1, agent_status: 'active', agent_responsibility: 'Conducts',
		}]);
		component = mount(Sidebar, {
			target: host,
			props: { open: true, onSelectThread: () => {}, onDeleteThread: () => {}, onOpenSettings: () => {}, conductorThreadId: 'cond', onConductor },
		});
		flushSync();
		host.querySelector('.scoped-item.conductor')!.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
		flushSync();
		const item = [...document.querySelectorAll('.context-menu button')].find((b) => b.textContent?.trim() === 'Agent settings');
		(item as HTMLButtonElement).click();
		await vi.waitFor(() => expect(onConductor).toHaveBeenCalled());
		await Promise.resolve();
		flushSync();
	}

	it('opens Agent settings once the conductor is open', async () => {
		threadSettingsOpen.set(false);
		await openAgentSettings(vi.fn().mockResolvedValue(true));
		expect(get(threadSettingsOpen)).toBe(true);
	});

	it('leaves Agent settings closed when the conductor could not be opened, so it never lands on another agent', async () => {
		threadSettingsOpen.set(false);
		await openAgentSettings(vi.fn().mockResolvedValue(false));
		expect(get(threadSettingsOpen)).toBe(false);
	});
});
