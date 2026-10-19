import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import CloudModelsOffSetting from './CloudModelsOffSetting.svelte';
import { token } from '$lib/stores/auth';
import { models, type Model } from '$lib/stores/models';

const DESCRIPTION = "When on, only models on this computer, or on a machine you've marked private, can run in this project, including every worker its agents start.";
const STOPS = 'Agents here using a cloud model stop at their next turn until they switch to a local one.';
const NONE_READY = 'No local model is ready, so nothing in this project can run until one is.';
const local = { id: 'omlx/q', name: 'Qwen', size: '', backend: 'network', speed: '', status: 'ready', is_local: true } as Model;
const cloud = { id: 'cc/opus', name: 'Opus', size: '', backend: 'claude-code', speed: '', status: 'unavailable', status_reason: 'project_cloud_off', is_local: false } as Model;

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let saves: unknown[] = [];

function stub(initial: boolean, put: 'ok' | 'fail' = 'ok') {
	vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
		if (init?.method === 'PUT') {
			saves.push(JSON.parse(init.body as string));
			return put === 'ok'
				? { ok: true, json: async () => JSON.parse(init.body as string) }
				: { ok: false, status: 500, json: async () => ({ detail: 'Server unavailable' }) };
		}
		return { ok: true, json: async () => ({ cloud_models_off: initial }) };
	}));
}
async function mountSetting() {
	component = mount(CloudModelsOffSetting, { target: host, props: { projectId: 'p1' } });
	flushSync();
	await vi.waitFor(() => expect((host.querySelector('button[role="switch"], input, button') as HTMLButtonElement | null)?.disabled).toBeFalsy());
}
const toggle = () => host.querySelector('[aria-label="Cloud models off"]') as HTMLElement;
const text = () => (host.textContent ?? '').replace(/\s+/g, ' ');

beforeEach(() => {
	host = document.createElement('div'); document.body.appendChild(host);
	token.set('tok'); models.set([local, cloud]); saves = [];
});
afterEach(() => { if (component) unmount(component); component = null; host.remove(); token.set(null); vi.unstubAllGlobals(); });

describe('CloudModelsOffSetting', () => {
	it('off: label and description only', async () => {
		stub(false); await mountSetting();
		expect(text()).toContain('Cloud models off');
		expect(text()).toContain(DESCRIPTION);
		expect(text()).not.toContain(STOPS);
		expect(host.querySelector('[role="alert"]')).toBeNull();
	});
	it('on with a local model ready: adds the stop line, no warning', async () => {
		stub(true); await mountSetting();
		await vi.waitFor(() => expect(text()).toContain(STOPS));
		expect(text()).not.toContain(NONE_READY);
	});
	it('on with no local model ready: amber line', async () => {
		models.set([cloud]); stub(true); await mountSetting();
		await vi.waitFor(() => expect(text()).toContain(NONE_READY));
		expect(host.querySelector('.msg.warn')).not.toBeNull();
	});
	it('turning it on saves and disables the switch until the save returns', async () => {
		let release!: () => void;
		vi.stubGlobal('fetch', vi.fn(async (_i: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PUT') { await new Promise<void>((r) => { release = r; }); return { ok: true, json: async () => ({ cloud_models_off: true }) }; }
			return { ok: true, json: async () => ({ cloud_models_off: false }) };
		}));
		await mountSetting();
		toggle().click(); flushSync();
		await vi.waitFor(() => expect((toggle() as HTMLButtonElement).disabled).toBe(true));
		release();
		await vi.waitFor(() => expect(text()).toContain(STOPS));
		expect((toggle() as HTMLButtonElement).disabled).toBe(false);
	});
	it('save failed: names the label, direction and error', async () => {
		stub(false, 'fail'); await mountSetting();
		toggle().click(); flushSync();
		await vi.waitFor(() => expect(text()).toContain("Couldn't save Cloud models off: Server unavailable"));
		expect(host.querySelector('.msg.err')).not.toBeNull();
		expect(text()).not.toContain(STOPS);
	});
	it('save failed turning it off: the same line, and the switch stays on', async () => {
		stub(true, 'fail'); await mountSetting();
		expect(toggle().getAttribute('aria-pressed')).toBe('true');
		toggle().click(); flushSync();
		await vi.waitFor(() => expect(text()).toContain("Couldn't save Cloud models off: Server unavailable"));
		expect(toggle().getAttribute('aria-pressed')).toBe('true');
	});
});
