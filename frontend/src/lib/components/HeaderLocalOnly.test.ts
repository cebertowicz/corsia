import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, threads } from '$lib/stores/threads';
import { canvasHeader } from '$lib/stores/canvasHeader';
import { uiScope } from '$lib/stores/uiScope';
import { models, type Model } from '$lib/stores/models';
import { onLocalOnlyFocusRequest } from '$lib/stores/modelSheetRequest';
import Header from './Header.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	canvasHeader.set(null);
	uiScope.set(null);
	models.set([]);
	threads.set([{
		id: 'thread-1', user_id: 'u', profile: 'personal', title: 'My thread',
		model: null, created_at: 'now', updated_at: 'now',
	}]);
	activeThreadId.set('thread-1');
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	threads.set([]);
	models.set([]);
});

function render(activeView: 'activity' | 'chat' = 'chat'): void {
	component = mount(Header, {
		target: host,
		props: { onToggleSidebar: () => {}, onTogglePane: () => {}, activeView, conductorThreadId: null },
	});
	flushSync();
}
const badge = () => host.querySelector<HTMLButtonElement>('.h-local-only');
const withLocalOnly = (fields: Record<string, unknown>) => threads.update((rows) => rows.map((row) => ({ ...row, ...fields })));

describe('Header Local only badge', () => {
	it('shows no badge on an ordinary chat but still reserves its slot', () => {
		render();
		expect(badge()).toBeNull();
		expect(host.querySelector('.h-local-slot')).not.toBeNull();
	});

	it.each([
		['its own', { local_only: 1 }],
		['inherited', { local_only: 1, local_only_inherited_from_thread_id: 'parent' }],
		['the project, by the thread payload', { local_only_effective: true }],
	])('shows the badge when Local only is %s', (_cause, fields) => {
		withLocalOnly(fields);
		render();
		expect(badge()?.getAttribute('aria-label')).toBe('Local only. Change in model settings.');
		expect(badge()?.textContent).toContain('Local only');
	});

	it('shows the badge when the catalog says the project allows only local models', () => {
		models.set([{ id: 'cc/opus', name: 'Opus', size: '', backend: 'claude-code', speed: '', status: 'unavailable', status_reason: 'project_cloud_off' } as Model]);
		render();
		expect(badge()).not.toBeNull();
	});

	it('asks for the model sheet with the Local only row focused when tapped', () => {
		withLocalOnly({ local_only: 1 });
		render();
		const focus = vi.fn();
		const stop = onLocalOnlyFocusRequest(focus);
		focus.mockClear();
		badge()!.click();
		stop();
		expect(focus).toHaveBeenCalledTimes(1);
	});

	it('shows no badge outside the chat view', () => {
		withLocalOnly({ local_only: 1 });
		render('activity');
		expect(badge()).toBeNull();
	});
});
