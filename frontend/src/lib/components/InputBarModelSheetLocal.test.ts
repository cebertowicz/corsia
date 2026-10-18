import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

import InputBarModelSheet from './InputBarModelSheet.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId, models, type Model } from '$lib/stores/models';
import { activeThreadId, threads } from '$lib/stores/threads';
import type { ModelSource } from '$lib/conductorSources';
import { toasts } from '$lib/stores/toast';

const claude: Model = { id: 'claude-code/sonnet', name: 'Sonnet', size: '', backend: 'claude-code', speed: '', status: 'ready' };
const localModel = { id: 'omlx/qwen', name: 'Qwen', size: '', backend: 'network', server_name: 'oMLX', litellm_model: 'omlx@qwen', speed: '', status: 'ready' } as Model;

let host: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let omlx: Partial<ModelSource> = {};
let calls: string[] = [];
let downloadBodies: unknown[] = [];
let setupBodies: unknown[] = [];
let setupResponses: Array<'consent' | 'failure' | 'ok'> = [];

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('tok');
	activeThreadId.set('t1');
	activeModelId.set(null);
	threads.set([]);
	models.set([claude]);
	calls = [];
	downloadBodies = [];
	setupBodies = [];
	setupResponses = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const key = `${init?.method ?? 'GET'} ${String(input)}`;
		calls.push(key);
		if (key === 'GET /api/providers/sources') {
			return { ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', ...omlx }] }) };
		}
		if (key === 'POST /api/providers/local/downloads') {
			downloadBodies.push(JSON.parse(String(init?.body)));
		}
		if (key === 'POST /api/providers/local/setup') {
			setupBodies.push(JSON.parse(String(init?.body)));
			const response = setupResponses.shift();
			if (response === 'consent') return { ok: false, status: 409, json: async () => ({ detail: { code: 'consent_required' } }) };
			if (response === 'failure') return { ok: false, status: 500, json: async () => ({}) };
		}
		return { ok: true, json: async () => ({}) };
	}));
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
	token.set(null);
	models.set([]);
	threads.set([]);
	activeThreadId.set(null);
});

async function render() {
	component = mount(InputBarModelSheet, { target: host });
	await vi.waitFor(() => expect(calls).toContain('GET /api/providers/sources'));
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

const button = (label: string) => [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);

describe('the picker\'s Local group', () => {
	it('offers the shared add-model sheet on an eligible Mac with no local rows', async () => {
		omlx = { eligible: true };
		await render();
		expect(button('Add a local model')).toBeDefined();
	});

	it('uses the shared status-less Local fallback header while the starter model downloads', async () => {
		omlx = { setup_stage: 'downloading', download_percent: 40 };
		await render();
		const fallback = host.querySelector('section[aria-label="Local"]')!;
		expect(fallback.querySelector('.model-group-bar')?.getAttribute('data-variant')).toBe('fallback');
		expect(fallback.querySelector('.model-group-bar')?.textContent?.trim()).toBe('Local');
		expect(fallback.textContent).toContain('Downloading 40%');
		expect(fallback.textContent).not.toContain('ready');
		expect(button('Try again')).toBeUndefined();
	});

	it('says only Downloading before a percent is known', async () => {
		omlx = { setup_stage: 'downloading', download_percent: null };
		await render();
		expect(host.textContent).toContain('Downloading');
		expect(host.textContent).not.toContain('%');
	});

	it('keeps the fallback Local header plain when setup fails', async () => {
		omlx = { setup_error: 'disk full', setup_kind: 'setup' };
		await render();
		const fallback = host.querySelector('section[aria-label="Local"]')!;
		expect(fallback.querySelector('.model-group-bar')?.textContent?.trim()).toBe('Local');
		expect(fallback.textContent).toContain('Setup failed');
		expect(fallback.textContent).not.toContain('ready');
	});

	it('retries a failed setup through the setup route', async () => {
		omlx = { setup_error: 'disk full', setup_kind: 'setup' };
		await render();
		button('Try again')!.click();
		await vi.waitFor(() => expect(calls).toContain('POST /api/providers/local/setup'));
		expect(calls).not.toContain('POST /api/providers/local/starter');
	});

	it('retries a failed starter download through the starter route, never the setup route', async () => {
		omlx = { setup_error: 'network down', setup_kind: 'starter' };
		await render();
		button('Try again')!.click();
		await vi.waitFor(() => expect(calls).toContain('POST /api/providers/local/starter'));
		expect(calls).not.toContain('POST /api/providers/local/setup');
	});

	it('shows a Settings download progress line with its model name and percent', async () => {
		omlx = { settings_download: { name: 'Qwen 3.8 27B', repository: 'Jundot/Qwen3.8-27B-oQ4e', percent: 42, failed: false } };
		await render();
		expect(host.textContent).toContain('Downloading Qwen 3.8 27B · 42%');
		expect(button('Try again')).toBeUndefined();
	});

	it('retries a durable Settings download failure with its stored repository', async () => {
		omlx = { settings_download: { name: 'Qwen 3.8 27B', repository: 'Jundot/Qwen3.8-27B-oQ4e', percent: 42, failed: true } };
		await render();
		expect(host.textContent).toContain("Couldn't download Qwen 3.8 27B.");
		button('Try again')!.click();
		await vi.waitFor(() => expect(calls).toContain('POST /api/providers/local/downloads'));
		expect(downloadBodies).toEqual([{ repository: 'Jundot/Qwen3.8-27B-oQ4e' }]);
		expect(calls).not.toContain('POST /api/providers/local/setup');
		expect(calls).not.toContain('POST /api/providers/local/starter');
	});

	it('offers informed consent in the picker and retries setup with it', async () => {
		omlx = { setup_error: 'disk full', setup_kind: 'setup' };
		await render();
		setupResponses = ['consent', 'ok'];
		button('Try again')!.click();
		await vi.waitFor(() => expect(setupBodies).toEqual([{ consent_existing: false }]));
		expect(setupResponses).toEqual(['ok']);
		await vi.waitFor(() => expect(get(toasts)).toHaveLength(1));
		expect(setupBodies).toEqual([{ consent_existing: false }]);
		await vi.waitFor(() => expect(get(toasts)[0]?.message).toBe('CORSIA can read the existing oMLX version, settings, and local server to adopt it without changing its settings.'));
		const consent = get(toasts)[0]!;
		expect(consent.action?.label).toBe('Use existing oMLX');

		await toasts.runAction(consent.id);

		expect(setupBodies).toEqual([{ consent_existing: false }, { consent_existing: true }]);
		expect(calls.filter((call) => call === 'GET /api/providers/sources')).toHaveLength(3);
	});

	it('shows a setup failure if the picker consent action cannot complete', async () => {
		omlx = { setup_error: 'disk full', setup_kind: 'setup' };
		await render();
		setupResponses = ['consent', 'failure'];
		button('Try again')!.click();
		await vi.waitFor(() => expect(get(toasts)).toHaveLength(1));
		await toasts.runAction(get(toasts)[0]!.id);
		await vi.waitFor(() => expect(get(toasts)[0]?.message).toBe("Couldn't set up the local model."));
	});

	it('shows a failure with no setup_kind without Try again', async () => {
		omlx = { setup_error: 'unknown' };
		await render();
		expect(host.textContent).toContain('Setup failed');
		expect(button('Try again')).toBeUndefined();
	});

	it('puts the line inside the existing Local group when it already lists models', async () => {
		models.set([claude, localModel]);
		omlx = { setup_stage: 'downloading', download_percent: 10 };
		await render();
		expect(host.querySelector('section[aria-label="Local"]')).toBeNull();
		expect(host.querySelector('section[aria-label="oMLX"]')?.textContent).toContain('Downloading 10%');
	});

	it('shows nothing extra when no download runs and nothing failed', async () => {
		omlx = {};
		await render();
		expect(host.textContent).not.toContain('Downloading');
		expect(host.textContent).not.toContain('Setup failed');
	});
});
