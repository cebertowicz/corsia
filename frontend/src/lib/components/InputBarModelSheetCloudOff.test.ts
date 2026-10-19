import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

import InputBarModelSheet from './InputBarModelSheet.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId, models, type Model } from '$lib/stores/models';
import { activeThreadId, threads } from '$lib/stores/threads';

const cloud = (id: string, name: string): Model => ({ id, name, size: '', backend: 'claude-code', provider: 'claude-code', speed: '', status: 'unavailable', status_reason: 'project_cloud_off', is_local: false } as Model);
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

describe('model sheet in a cloud-off project', () => {
	it('some local models ready: only local rows, then the quiet line', () => {
		models.set([cloud('cc/opus', 'Claude Opus'), local]); open();
		expect(text()).toContain('Qwen'); expect(text()).not.toContain('Claude Opus');
		expect(text()).toContain('Cloud models are off in this project.');
		expect(text()).not.toContain('no local model is ready');
	});
	it('no local model: the empty state with its link', () => {
		models.set([cloud('cc/opus', 'Claude Opus')]); open();
		expect(text()).toContain('Cloud models are off in this project, and no local model is ready.');
		expect(Array.from(host.querySelectorAll('button')).some((b) => b.textContent?.trim() === 'Open project settings')).toBe(true);
		expect(text()).not.toContain('Claude Opus');
	});
	it('not a cloud-off project: unchanged', () => {
		models.set([{ id: 'ext/gpt', name: 'Claude Opus', size: '', backend: 'network', provider: 'external', server_name: 'Hosted', litellm_model: 'external@h/gpt', speed: '', status: 'ready', is_local: false } as Model, local]); open();
		expect(text()).toContain('Claude Opus'); expect(text()).not.toContain('Cloud models are off');
	});
});
