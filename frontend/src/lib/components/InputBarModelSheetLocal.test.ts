import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

import InputBarModelSheet from './InputBarModelSheet.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId, models, type Model } from '$lib/stores/models';
import { activeThreadId, threads } from '$lib/stores/threads';
import type { ModelSource } from '$lib/conductorSources';

const claude: Model = { id: 'claude-code/sonnet', name: 'Sonnet', size: '', backend: 'claude-code', speed: '', status: 'ready' };
const localModel = { id: 'omlx/qwen', name: 'Qwen', size: '', backend: 'network', server_name: 'oMLX', litellm_model: 'omlx@qwen', speed: '', status: 'ready' } as Model;

let host: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let omlx: Partial<ModelSource> = {};
let calls: string[] = [];

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('tok');
	activeThreadId.set('t1');
	activeModelId.set(null);
	threads.set([]);
	models.set([claude]);
	calls = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const key = `${init?.method ?? 'GET'} ${String(input)}`;
		calls.push(key);
		if (key === 'GET /api/providers/sources') {
			return { ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', ...omlx }] }) };
		}
		return { ok: true, json: async () => ({}) };
	}));
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
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
