import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { activeThreadId, threads } from '$lib/stores/threads';
import { canvasHeader } from '$lib/stores/canvasHeader';
import { uiScope } from '$lib/stores/uiScope';
import { models, type Model } from '$lib/stores/models';
import { onModelSheetRequest } from '$lib/stores/modelSheetRequest';
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
	it('shows no badge or slot on an ordinary chat', () => {
		render();
		expect(badge()).toBeNull();
		expect(host.querySelector('.h-local-slot')).toBeNull();
	});

	it.each([
		['its own', { local_only: 1, local_only_effective: true, local_only_cause: 'own' }],
		['inherited', { local_only: 1, local_only_effective: true, local_only_cause: 'inherited', local_only_inherited_from_thread_id: 'parent' }],
		['the project, by the thread payload', { local_only_effective: true, local_only_cause: 'project' }],
	])('shows the badge when Local only is %s', (_cause, fields) => {
		withLocalOnly(fields);
		render();
		expect(host.querySelector('.h-local-slot')).not.toBeNull();
		expect(badge()?.getAttribute('aria-label')).toBe('Local only. Change in model settings.');
		expect(badge()?.textContent).toContain('Local only');
	});

	it('does not infer the badge from catalog availability', () => {
		models.set([{ id: 'cc/opus', name: 'Opus', size: '', backend: 'claude-code', speed: '', status: 'unavailable', status_reason: 'project_cloud_off' } as Model]);
		render();
		expect(badge()).toBeNull();
	});

	it('asks for the model sheet with the Local only row focused when tapped', () => {
		withLocalOnly({ local_only: 1, local_only_effective: true, local_only_cause: 'own' });
		render();
		const open = vi.fn();
		const stop = onModelSheetRequest(open);
		badge()!.click();
		stop();
		expect(open).toHaveBeenCalledWith(expect.objectContaining({ focusLocalOnly: true }));
	});

	it('shows no badge outside the chat view', () => {
		withLocalOnly({ local_only: 1, local_only_effective: true, local_only_cause: 'own' });
		render('activity');
		expect(badge()).toBeNull();
	});
});
