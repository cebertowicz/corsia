import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
vi.mock('$lib/stores/localPickerStatus', () => ({
	localStatus: { subscribe: (run: (value: unknown) => void) => { run(null); return () => {}; } },
	canAddLocalModel: { subscribe: (run: (value: unknown) => void) => { run(false); return () => {}; } },
	retryLocalSetup: vi.fn(), watchLocalStatus: () => () => {},
}));

import InputBarModelSheet from './InputBarModelSheet.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId, models, type Model } from '$lib/stores/models';
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
afterEach(() => { if (component) unmount(component); component = null; host.remove(); token.set(null); vi.unstubAllGlobals(); });
const open = () => { component = mount(InputBarModelSheet, { target: host }); flushSync(); };

describe('model sheet for a Local only thread', () => {
	it('hides rows the thread refuses through the same filter as the project cause', () => {
		models.set([cloud('thread_local_only'), local]); open();
		expect(text()).toContain('Qwen'); expect(text()).not.toContain('Claude Opus');
	});
	it('names the project from the thread payload, never from catalog availability', () => {
		threads.set([{ id: 't1', project_id: 'p1', local_only: 1, local_only_from_project: true, updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('thread_local_only'), local]); open();
		expect(text()).toContain('This project uses local models only.');
		unmount(component!); component = null; host.innerHTML = '';
		threads.set([{ id: 't1', project_id: 'p1', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('project_cloud_off'), local]); open();
		expect(text()).not.toContain('This project uses local models only.');
	});
	it('mounts the Local only row above the model list, and its own state has no quiet line', () => {
		threads.set([{ id: 't1', project_id: 'p1', local_only: 1, updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('thread_local_only'), local]); open();
		expect(host.innerHTML.indexOf('local-only-setting')).toBeLessThan(host.innerHTML.indexOf('sheet-section-label'));
		expect(text()).toContain('This chat and its agents use local models only.');
		expect(host.querySelector('.model-list')?.textContent).not.toContain('use local models only');
	});
	it('an inherited worker keeps a quiet line under the list that names the scope', () => {
		threads.set([{ id: 't1', project_id: 'p1', local_only: 1, local_only_inherited_from_thread_id: 'p0', local_only_inherited_from_title: 'Quarterly report', updated_at: '2026-10-19T10:00:00.000000' } as never]);
		models.set([cloud('thread_local_only'), local]); open();
		expect(host.querySelector('.model-list')?.textContent).toContain('This chat and its agents use local models only.');
		expect(text()).toContain('Set by Quarterly report');
	});
	it('no local model: one empty state with the thread wording and the add-local entry point', () => {
		models.set([cloud('thread_local_only')]); open();
		expect(text()).toContain('No local model is ready.');
		expect(text()).not.toContain('This project');
		const link = Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Set up a local model');
		expect(link).toBeTruthy();
		link!.click(); flushSync();
		expect(document.querySelector('[role="dialog"]')).toBeTruthy();
	});
});
