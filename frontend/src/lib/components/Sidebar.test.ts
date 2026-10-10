import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import Sidebar from './Sidebar.svelte';
import { activeThreadId, threads } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';
import { measurementScope } from '$lib/stores/measurement';
import { measurementOrigin } from '$lib/stores/measurementNavigation';

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
