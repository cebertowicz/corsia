import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
const setup = vi.hoisted(() => ({ status: { value: null as null | { line: string; retry: null } }, canAdd: { value: false } }));
vi.mock('$lib/stores/localPickerStatus', async () => {
	return {
		localStatus: { subscribe: (run: (value: unknown) => void) => { run(setup.status.value); return () => {}; } },
		canAddLocalModel: { subscribe: (run: (value: unknown) => void) => { run(setup.canAdd.value); return () => {}; } },
		retryLocalSetup: vi.fn(), watchLocalStatus: () => () => {},
	};
});

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
	setup.status.value = null; setup.canAdd.value = false;
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })));
});
afterEach(() => { if (component) unmount(component); component = null; host.remove(); token.set(null); vi.unstubAllGlobals(); });
const open = () => { component = mount(InputBarModelSheet, { target: host }); flushSync(); };

describe('model sheet in a cloud-off project', () => {
	it('some local models ready: only local rows, then the quiet line', () => {
		models.set([cloud('cc/opus', 'Claude Opus'), local]); open();
		expect(text()).toContain('Qwen'); expect(text()).not.toContain('Claude Opus');
		expect(text()).toContain('This project uses local models only.');
		expect(text()).not.toContain('no local model is ready');
	});
	it('no local model: the empty state with its link', () => {
		models.set([cloud('cc/opus', 'Claude Opus')]); open();
		expect(text()).toContain('This project uses local models only, and no local model is ready.');
		expect(Array.from(host.querySelectorAll('button')).some((b) => b.textContent?.trim() === 'Open project settings')).toBe(true);
		expect(text()).not.toContain('Claude Opus');
	});
	it('no local model at all: the line and link, then the Local setup section with Add a local model', () => {
		setup.canAdd.value = true;
		models.set([cloud('cc/opus', 'Claude Opus')]); open();
		expect(text()).toContain('This project uses local models only, and no local model is ready.');
		expect(text()).not.toContain('No available models');
		const buttons = Array.from(host.querySelectorAll('button')).map((b) => b.textContent?.trim());
		expect(buttons).toContain('Open project settings'); expect(buttons).toContain('Add a local model');
		expect(host.innerHTML.indexOf('no local model is ready')).toBeLessThan(host.innerHTML.indexOf('aria-label="Local"'));
	});
	it('no local model at all, this computer setting up: its status shows below the line', () => {
		setup.status.value = { line: 'Setting up the local model…', retry: null };
		models.set([cloud('cc/opus', 'Claude Opus')]); open();
		expect(text()).toContain('no local model is ready'); expect(text()).toContain('Setting up the local model…');
	});
	it('not cloud-off and nothing at all: No available models', () => {
		models.set([]); open();
		expect(text()).toContain('No available models');
	});
	it('only a not-ready local row: the line and link show and the group is still listed', () => {
		models.set([cloud('cc/opus', 'Claude Opus'), { ...local, status: 'unavailable', status_reason: 'connection_unavailable' } as Model]); open();
		expect(text()).toContain('This project uses local models only, and no local model is ready.');
		expect(Array.from(host.querySelectorAll('button')).some((b) => b.textContent?.trim() === 'Open project settings')).toBe(true);
		expect(host.querySelector('section[aria-label="This Mac"]')).not.toBeNull(); expect(text()).not.toContain('Claude Opus');
		expect(host.innerHTML.indexOf('no local model is ready')).toBeLessThan(host.innerHTML.indexOf('aria-label="This Mac"'));
	});
	it('a ready local row: no empty-state line', () => {
		models.set([cloud('cc/opus', 'Claude Opus'), local]); open();
		expect(text()).not.toContain('no local model is ready');
		expect(Array.from(host.querySelectorAll('button')).some((b) => b.textContent?.trim() === 'Open project settings')).toBe(false);
	});
	it('not a cloud-off project: unchanged', () => {
		models.set([{ id: 'ext/gpt', name: 'Claude Opus', size: '', backend: 'network', provider: 'external', server_name: 'Hosted', litellm_model: 'external@h/gpt', speed: '', status: 'ready', is_local: false } as Model, local]); open();
		expect(text()).toContain('Claude Opus'); expect(text()).not.toContain('This project uses local models only.');
	});
});

describe('a private machine is not this computer', () => {
	const external = { id: 'ext/lms', name: 'Gemma', size: '', backend: 'network', provider: 'external', server_name: 'LM Studio', litellm_model: 'external@lms/gemma', speed: '', status: 'ready', is_local: true } as Model;
	it('private External group only: Local header, and the Local setup section shows below it', () => {
		setup.canAdd.value = true;
		models.set([cloud('cc/opus', 'Claude Opus'), external]); open();
		expect(text()).toContain('Gemma'); expect(text()).toContain('Local · ready');
		expect(host.innerHTML.indexOf('aria-label="LM Studio"')).toBeLessThan(host.innerHTML.indexOf('aria-label="Local"'));
		expect(Array.from(host.querySelectorAll('button')).map((b) => b.textContent?.trim())).toContain('Add a local model');
	});
	it('private External group never shows this computer\'s setup status inside its own group', () => {
		setup.status.value = { line: 'Setting up the local model…', retry: null };
		models.set([external]); open();
		expect(host.querySelector('section[aria-label="LM Studio"]')?.textContent).not.toContain('Setting up the local model…');
		expect(host.querySelector('section[aria-label="Local"]')?.textContent).toContain('Setting up the local model…');
	});
});

describe('Local header follows the backend is_local flag', () => {
	const remote = (isLocal: boolean): Model => ({ id: 'omlx/llama', name: 'Llama', size: '', backend: 'network', provider: 'omlx', server_name: 'Studio mini', litellm_model: 'omlx@llama', speed: '', status: 'ready', is_local: isLocal } as Model);
	it('remote oMLX not marked private: no Local label', () => {
		models.set([remote(false)]); open();
		expect(text()).toContain('Llama'); expect(text()).not.toContain('Local · ready');
	});
	it('the same group marked private: Local label', () => {
		models.set([remote(true)]); open();
		expect(text()).toContain('Local · ready');
	});
	it('External group with is_local: Local label', () => {
		models.set([{ id: 'ext/lms', name: 'Gemma', size: '', backend: 'network', provider: 'external', server_name: 'LM Studio', litellm_model: 'external@lms/gemma', speed: '', status: 'ready', is_local: true } as Model]); open();
		expect(text()).toContain('Local · ready');
	});
});
