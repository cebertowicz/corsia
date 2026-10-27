import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ConductorSourcesPanel from './ConductorSourcesPanel.svelte';
import { token } from '$lib/stores/auth';
import { messages } from '$lib/stores/threads';
import { loadModels, models, saveThreadModel, type Model } from '$lib/stores/models';
import type { ModelSource, SourceAccount } from '$lib/conductorSources';
import { emit } from '$lib/ws/events';

vi.mock('$lib/stores/models', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/stores/models')>();
	return { ...actual, loadModels: vi.fn(async () => {}), saveThreadModel: vi.fn() };
});

function account(state: SourceAccount['state'], extra: Partial<SourceAccount> = {}): SourceAccount {
	return { account_id: 'acc-1', label: 'me@example.com', label_verified: true, identified: true, state, resets_at: null, corsia_agent_binding: false, ...extra };
}

function claude(state: ModelSource['state'], accounts: SourceAccount[] = []): ModelSource {
	return { source: 'claude-code', name: 'Claude Code', state, accounts };
}

const CATALOG: Model[] = [{ id: 'claude-code/sonnet', name: 'Sonnet', size: '', backend: 'claude-code', speed: '', status: 'ready' }];

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let listed: ModelSource[] = [];
let calls: string[] = [];
let readFails = false;

function button(label: string): HTMLButtonElement | undefined {
	return [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);
}

function text(): string {
	flushSync();
	return (host.textContent ?? '').replace(/\s+/g, ' ').trim();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('tok');
	models.set(CATALOG);
	messages.set([]);
	calls = [];
	readFails = false;
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const key = `${init?.method ?? 'GET'} ${String(input)}`;
		calls.push(key);
		if (key === 'GET /api/providers/sources') return { ok: !readFails, status: readFails ? 500 : 200, json: async () => ({ sources: listed }) };
		throw new Error(`Unexpected fetch: ${key}`);
	}));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	vi.mocked(loadModels).mockReset();
	vi.mocked(saveThreadModel).mockReset();
	models.set([]);
	messages.set([]);
});

async function render() {
	component = mount(ConductorSourcesPanel, { target: host, props: { threadId: 'cond-1' } });
	await vi.waitFor(() => expect(text()).not.toContain('Checking which models are ready'));
}

describe('first-run line', () => {
	it('says Checking while the sources are read', () => {
		listed = [];
		component = mount(ConductorSourcesPanel, { target: host, props: { threadId: 'cond-1' } });
		expect(text()).toContain('Checking which models are ready…');
	});

	it('with a ready model: points at the picker and shows no button or link', async () => {
		listed = [claude('ready', [account('ready')])];
		await render();
		expect(text()).toBe('Pick a model from the picker below to start CORSIA.');
		expect(host.querySelectorAll('button')).toHaveLength(0);
		expect(saveThreadModel).not.toHaveBeenCalled();
	});

	it('with nothing ready (signed out, limited, not installed): renders nothing', async () => {
		const limited = account('limited', { resets_at: new Date(Date.now() + 3600_000).toISOString() });
		for (const sources of [
			[claude('needs_auth', [account('signed_out')])],
			[claude('ready', [limited])],
			[claude('not_installed')],
		]) {
			listed = sources;
			await render();
			await vi.waitFor(() => expect(calls).toContain('GET /api/providers/sources'));
			expect(text()).toBe('');
			expect(host.querySelector('section')).toBeNull();
			expect(text()).not.toContain('No models are ready yet');
			unmount(component!);
			component = null;
		}
	});

	it('shows no per-source cards or capacity note', async () => {
		listed = [claude('ready', [account('ready')])];
		await render();
		expect(host.querySelectorAll('article')).toHaveLength(0);
		expect(text()).not.toContain('Agent teams use a lot of tokens.');
	});

	it('keeps the typed-first note', async () => {
		listed = [claude('ready', [account('ready')])];
		messages.set([{ id: 'm1', role: 'user', content: 'hello' }] as never);
		await render();
		expect(text()).toContain("I'll answer once you start me.");
	});

	it('shows an initial read failure until Retry succeeds', async () => {
		readFails = true;
		component = mount(ConductorSourcesPanel, { target: host, props: { threadId: 'cond-1' } });
		await vi.waitFor(() => expect(text()).toContain("Couldn't read your model sources."));
		listed = [claude('ready', [account('ready')])];
		readFails = false;
		button('Retry')!.click();
		await vi.waitFor(() => expect(text()).toContain('Pick a model from the picker below'));
		expect(text()).not.toContain("Couldn't read your model sources.");
	});

	it('flips the line on models_updated, such as a download finishing', async () => {
		listed = [{ source: 'omlx', name: 'Local model', state: 'needs_setup', eligible: true, local_state: 'downloading', download_percent: 40 }];
		await render();
		await vi.waitFor(() => expect(host.querySelector('section')).toBeNull());
		listed = [{ source: 'omlx', name: 'Local model', state: 'ready' }];
		emit('models_updated', {});
		await vi.waitFor(() => expect(text()).toContain('Pick a model from the picker below'));
		expect(calls.filter((c) => c === 'GET /api/providers/sources')).toHaveLength(2);
	});

	it('has an accessible region name and no emoji', async () => {
		listed = [claude('ready', [account('ready')])];
		await render();
		expect(host.querySelector('section[aria-label="Model sources"]')).not.toBeNull();
		expect(/\p{Extended_Pictographic}/u.test(host.textContent ?? '')).toBe(false);
	});
});
