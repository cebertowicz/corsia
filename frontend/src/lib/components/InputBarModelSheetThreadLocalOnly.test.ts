import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
const localPicker = vi.hoisted(() => ({ canAdd: false }));
vi.mock('$lib/stores/localPickerStatus', () => ({
	localStatus: { subscribe: (run: (value: unknown) => void) => { run(null); return () => {}; } },
	canAddLocalModel: { subscribe: (run: (value: unknown) => void) => { run(localPicker.canAdd); return () => {}; } },
	canSetUpLocalModel: { subscribe: (run: (value: unknown) => void) => { run(false); return () => {}; } },
	retryLocalSetup: vi.fn(), watchLocalStatus: () => () => {},
}));

import InputBarModelSheet from './InputBarModelSheet.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId, models, type Model } from '$lib/stores/models';
import { threadCatalogs } from '$lib/stores/modelsState';
import { activeThreadId, threads } from '$lib/stores/threads';

const cloud = (reason: string): Model => ({ id: 'cc/opus', name: 'Claude Opus', size: '', backend: 'claude-code', provider: 'claude-code', speed: '', status: 'unavailable', status_reason: reason, is_local: false } as Model);
const local = { id: 'omlx/qwen', name: 'Qwen', size: '', backend: 'network', provider: 'omlx', server_name: 'This Mac', litellm_model: 'omlx@qwen', speed: '', status: 'ready', is_local: true } as Model;

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const text = () => (host.textContent ?? '').replace(/\s+/g, ' ');

beforeEach(() => {
	host = document.createElement('div'); document.body.appendChild(host);
	token.set('tok'); activeThreadId.set('t1'); activeModelId.set(null);
	threads.set([{ id: 't1', project_id: 'p1', updated_at: '2026-10-19T10:00:00.000000' } as never]);
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })));
});
afterEach(() => { localPicker.canAdd = false; threadCatalogs.set(new Map()); if (component) unmount(component); component = null; host.remove(); token.set(null); vi.unstubAllGlobals(); });
const addLinks = () => Array.from(host.querySelectorAll('button')).filter((b) => ['Add a local model', 'Set up a local model'].includes(b.textContent?.trim() ?? ''));
const open = (props: Record<string, unknown> = {}) => { component = mount(InputBarModelSheet, { target: host, props }); flushSync(); };

describe('model sheet for a Local only thread', () => {
	it('hides rows the thread refuses through the same filter as the project cause', () => {
		models.set([cloud('thread_local_only'), local]); open();
		expect(text()).toContain('Qwen'); expect(text()).not.toContain('Claude Opus');
	});
	it('names the project from the thread payload, never from catalog availability', () => {
		threads.set([{ id: 't1', project_id: 'p1', local_only: 0, local_only_effective: true, local_only_cause: 'project', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('thread_local_only'), local]); open();
		expect(text()).toContain('This project uses local models only.');
		unmount(component!); component = null; host.innerHTML = '';
		threads.set([{ id: 't1', project_id: 'p1', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('project_cloud_off'), local]); open();
		expect(text()).not.toContain('This project uses local models only.');
	});
	it('mounts the Local only row above the model list, and its own state has no quiet line', () => {
		threads.set([{ id: 't1', project_id: 'p1', local_only: 1, local_only_effective: true, local_only_cause: 'own', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('thread_local_only'), local]); open();
		expect(host.innerHTML.indexOf('local-only-setting')).toBeLessThan(host.innerHTML.indexOf('sheet-section-label'));
		expect(text()).toContain('This chat and its agents use local models only.');
		expect(host.querySelector('.model-list')?.textContent).not.toContain('use local models only');
	});
	it('an inherited worker keeps a quiet line under the list that names the scope', () => {
		threads.set([{ id: 't1', project_id: 'p1', local_only: 1, local_only_effective: true, local_only_cause: 'inherited', local_only_inherited_from_thread_id: 'p0', local_only_inherited_from_title: 'Quarterly report', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('thread_local_only'), local]); open();
		expect(host.querySelector('.model-list')?.textContent).toContain('This chat and its agents use local models only.');
		expect(text()).toContain('Set by Quarterly report');
	});
	it('no local model: one empty state with the thread wording and the add-local entry point', () => {
		models.set([cloud('thread_local_only')]); open();
		expect(text()).toContain('No local model is ready.');
		expect(text()).not.toContain('This project');
		const links = addLinks();
		expect(links).toHaveLength(1); expect(links[0].classList.contains('local-only-link')).toBe(true);
		links[0].click(); flushSync();
		expect(document.querySelector('[role="dialog"]')).toBeTruthy();
	});
	it('no local model, Local section shown: its add link is the only way to add one', () => {
		localPicker.canAdd = true; models.set([cloud('thread_local_only')]); open();
		expect(text()).toContain('No local model is ready.');
		expect(addLinks()).toHaveLength(1); expect(host.querySelector('[aria-label="Local"] .add-local-link')).toBeTruthy();
	});
	it('a project-held chat with no local model: the row names the project and claims no add link', () => {
		threads.set([{ id: 't1', project_id: 'p1', local_only: 0, local_only_effective: true, local_only_cause: 'project', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('thread_local_only')]); open();
		expect(text()).toContain('Set by this project');
		expect(addLinks()).toHaveLength(0);
		expect(Array.from(host.querySelectorAll('button')).some((b) => b.textContent?.trim() === 'Open project settings')).toBe(true);
	});
	it('an inherited chat with no local model: the row names its parent, so the note carries the one link', () => {
		threads.set([{ id: 't1', project_id: 'p1', local_only: 1, local_only_effective: true, local_only_cause: 'inherited', local_only_inherited_from_thread_id: 'p0', local_only_inherited_from_title: 'Quarterly report', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('thread_local_only')]); open();
		expect(text()).toContain('Set by Quarterly report');
		const links = addLinks();
		expect(links).toHaveLength(1); expect(links[0].classList.contains('local-only-link')).toBe(false);
	});
	it('a worker composer with no Local only row and no Local section keeps the note\'s add link', () => {
		threads.set([{ id: 't1', project_id: 'p1', updated_at: '2026-10-19T10:00:00.000000' } as never, { id: 'w1', project_id: 'p1', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		const setup = { ...local, id: 'omlx/setup', litellm_model: 'omlx@ns-1/setup', status: 'unavailable' } as Model;
		threadCatalogs.set(new Map([['w1', [cloud('thread_local_only'), setup]]]));
		open({ threadId: 'w1' });
		expect(text()).not.toContain('Local only'); expect(host.querySelector('[aria-label="Local"]')).toBeNull();
		expect(addLinks()).toHaveLength(1);
	});
});
