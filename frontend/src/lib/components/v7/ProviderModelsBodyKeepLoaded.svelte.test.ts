import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ProviderModelsBody from './ProviderModelsBody.svelte';
import { token } from '$lib/stores/auth';
import type { HostProvider } from '$lib/stores/providersV7';

const GIB = 1024 ** 3;
const localProvider = {
	name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: 'Local',
	raw: { name: 'omlx', label: 'oMLX', type: 'network' },
	server: { id: 'local-test', provider: 'omlx', kind: 'managed', source: 'manual', corsia_omlx: true, on_this_mac: true, status: 'online', instance_name: 'Local', endpoints: [] },
} as unknown as HostProvider;

// The backend decides whether pinning fits (``pin_fits``) and writes the line under the row (``keep_line``); the rows carry its verdicts.
type Row = { id: string; display_name: string; size_bytes: number; pinned?: boolean; starting?: boolean; pin_fits?: boolean; keep_line?: string };
const row = (id: string, gb: number, extra: Partial<Row> = {}): Row => ({ id, display_name: id, size_bytes: gb * GIB, ...extra });
const NOT_LOADED = 'Not loaded: needs 12 GB, 8 GB free for a model now';
const PINS_FULL = "Models kept loaded use 14 of 20 GB. Turn one off to keep this one loaded. It's loaded, but won't load at the next start.";

let component: ReturnType<typeof mount> | null = null;
let calls: Array<{ method: string; url: string; body: unknown }> = [];

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
	token.set(null);
});

async function render({ loaded = [], available = [], checkOk = true, provider = localProvider }: { loaded?: Row[]; available?: Row[]; checkOk?: boolean; provider?: HostProvider }) {
	calls = [];
	token.set('test-token');
	vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
		const method = init?.method ?? 'GET';
		if (method !== 'GET') calls.push({ method, url: url.split('?')[0], body: init?.body ? JSON.parse(String(init.body)) : null });
		if (url.includes('/network-servers/local-test/models') && method === 'GET') {
			return { ok: true, json: async () => ({ loaded: loaded.map((model) => ({ ...model, model_id: model.id })), available: available.map((model) => ({ ...model, model_id: model.id })) }) };
		}
		if (url.includes('/host-resources/check-load')) return { ok: true, json: async () => checkOk ? { ok: true } : { ok: false, free_gb: 8, after_load_gb: -2, safe_reserve_gb: 4, warning: 'Too big' } };
		if (url.includes('/providers/local/downloads')) return { ok: true, json: async () => [] };
		if (url.includes('/providers/sources')) return { ok: true, json: async () => ({ sources: [] }) };
		return { ok: true, json: async () => ({}) };
	}));
	component = mount(ProviderModelsBody, { target: document.body, props: { provider, isLocal: true } });
	flushSync();
	const names = [...loaded, ...available].map((model) => model.id).sort().join();
	await vi.waitFor(() => expect([...document.querySelectorAll('.mname')].map((item) => item.textContent).sort().join()).toBe(names));
	await tick(); await tick();
}

const rowFor = (name: string) => [...document.querySelectorAll('.mr')].find((item) => item.querySelector('.mname')?.textContent === name) as HTMLElement;
const switchOf = (name: string) => rowFor(name).querySelector('button.tg') as HTMLButtonElement;
const pinWrites = () => calls.filter((call) => call.url.endsWith('/settings'));
// The memory check is a POST too; it is not a change.
const changes = () => calls.filter((call) => !call.url.includes('check-load'));
const lastTwo = (url: string) => url.split('/').slice(-2).join('/');

describe('local model switch: keep this model loaded', () => {
	it('is on for a loaded or a pinned model, and off for the rest', async () => {
		await render({ loaded: [row('loaded-one', 4)], available: [row('pinned-one', 4, { pinned: true }), row('plain-one', 4)] });
		expect(switchOf('loaded-one').classList.contains('on')).toBe(true);
		expect(switchOf('pinned-one').classList.contains('on')).toBe(true);
		expect(switchOf('plain-one').classList.contains('on')).toBe(false);
	});

	it('turning on loads the model, then pins it when the pins fit', async () => {
		await render({ available: [row('turn-on', 4, { pin_fits: true })] });
		switchOf('turn-on').click();
		await vi.waitFor(() => expect(pinWrites()).toHaveLength(1));
		expect(changes().map((call) => lastTwo(call.url))).toEqual(['models/load', 'turn-on/settings']);
		expect(pinWrites()[0].body).toEqual({ pinned: true });
	});

	it('loads without pinning when the backend says the pins would not fit', async () => {
		await render({ available: [row('pinned-a', 14, { pinned: true, pin_fits: true }), row('too-many', 8, { pin_fits: false })] });
		switchOf('too-many').click();
		await vi.waitFor(() => expect(calls.some((call) => call.url.endsWith('/models/load'))).toBe(true));
		await tick(); await tick();
		expect(pinWrites()).toHaveLength(0);
	});

	it('cancelling the memory check changes nothing: no load, no pin', async () => {
		await render({ available: [row('cancel-me', 4, { pin_fits: true })], checkOk: false });
		switchOf('cancel-me').click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('Too big'));
		[...document.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Cancel')!.click();
		await tick(); await tick(); flushSync();
		expect(changes()).toEqual([]);
		// The row goes back to off: no Loading state or row Cancel is left behind.
		expect([...rowFor('cancel-me').querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Cancel')).toBe(false);
		expect(switchOf('cancel-me').classList.contains('on')).toBe(false);
	});

	it('turning off unpins a pinned model, then unloads it', async () => {
		await render({ loaded: [row('keeper', 4, { pinned: true })] });
		switchOf('keeper').click();
		await vi.waitFor(() => expect(changes()).toHaveLength(2));
		expect(changes()[0].url.endsWith('/keeper/settings')).toBe(true);
		expect(changes()[0].body).toEqual({ pinned: false });
		expect(changes()[1].url.endsWith('/models/unload')).toBe(true);
	});
});

describe("a person's own oMLX", () => {
	for (const [where, on_this_mac] of [['on another computer', false], ['on this computer', true]] as const) {
		it(`keeps Load and Unload ${where}: CORSIA never pins a model on it`, async () => {
			const theirs = { ...localProvider, server: { ...localProvider.server, corsia_omlx: false, on_this_mac } } as unknown as HostProvider;
			await render({ loaded: [row('theirs', 4, { pinned: true })], provider: theirs });
			switchOf('theirs').click();
			await vi.waitFor(() => expect(changes().length).toBeGreaterThan(0));
			expect(changes().map((call) => lastTwo(call.url))).toEqual(['models/unload']);
			expect(pinWrites()).toHaveLength(0);
		});
	}
});

describe('local model lines', () => {
	it("shows the backend's pins-full line on a loaded model that could not be pinned", async () => {
		await render({ loaded: [row('loaded-one', 8, { pin_fits: false, keep_line: PINS_FULL })], available: [row('pinned-a', 14, { pinned: true })] });
		await vi.waitFor(() => expect(rowFor('loaded-one').textContent).toContain(PINS_FULL));
		expect(rowFor('pinned-a').textContent).not.toContain('Models kept loaded use');
	});

	it("shows the backend's not-loaded line on a pinned model that doesn't fit now, and offers Load anyway", async () => {
		await render({ available: [row('pinned-big', 12, { pinned: true, keep_line: NOT_LOADED })] });
		await vi.waitFor(() => expect(rowFor('pinned-big').textContent).toContain(NOT_LOADED));
		expect(switchOf('pinned-big').classList.contains('on')).toBe(true);
		const anyway = [...rowFor('pinned-big').querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Load anyway')!;
		anyway.click();
		await vi.waitFor(() => expect(calls.some((call) => call.url.endsWith('/models/load'))).toBe(true));
		expect(pinWrites()).toHaveLength(0);
	});

	it("shows the backend's line for a keep attempt that failed, on a model that isn't pinned, without Load anyway", async () => {
		const notKept = "Downloaded, but couldn't keep it loaded. Turn it on to try again.";
		await render({ available: [row('new-one', 4, { pin_fits: true, keep_line: notKept })] });
		await vi.waitFor(() => expect(rowFor('new-one').textContent).toContain(notKept));
		expect([...rowFor('new-one').querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Load anyway')).toBe(false);
	});

	it('shows neither line for an unpinned, unloaded model', async () => {
		await render({ available: [row('plain-one', 12)] });
		expect(rowFor('plain-one').textContent).not.toContain('Not loaded');
		expect([...rowFor('plain-one').querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Load anyway')).toBe(false);
	});

	it('says nothing is wrong for a pinned model that fits now', async () => {
		await render({ available: [row('pinned-small', 4, { pinned: true })] });
		expect(rowFor('pinned-small').textContent).not.toContain('Not loaded');
	});

	it('shows a pinned model oMLX is still loading at start as Loading, with its switch on and off-limits and no Not loaded line', async () => {
		await render({ available: [row('starting-up', 12, { pinned: true, starting: true })] });
		expect(document.querySelector('.section.loading')).not.toBeNull();
		const buttons = [...rowFor('starting-up').querySelectorAll('button')].map((button) => button.textContent?.trim());
		expect(rowFor('starting-up').textContent).not.toContain('Not loaded');
		expect(buttons).not.toContain('Load anyway');
		// There is no job to cancel while oMLX preloads.
		expect(buttons).not.toContain('Cancel');
		// The person turned it on (the switch is the pin): it reads on, and can't be used until it loads.
		expect(switchOf('starting-up').classList.contains('on')).toBe(true);
		expect(switchOf('starting-up').disabled).toBe(true);
	});

	it('keeps the switch on and off-limits on a row loading after the person turned it on', async () => {
		await render({ available: [row('turn-on', 4, { pin_fits: true })] });
		switchOf('turn-on').click();
		await vi.waitFor(() => expect(document.querySelector('.section.loading')).not.toBeNull());
		expect(switchOf('turn-on').classList.contains('on')).toBe(true);
		expect(switchOf('turn-on').disabled).toBe(true);
	});

	it('reads the list again until oMLX finishes its start preload, then shows why a pin did not load', async () => {
		const waiting = row('starting-up', 12, { pinned: true, starting: true });
		await render({ available: [waiting] });
		waiting.starting = false;
		waiting.keep_line = NOT_LOADED;
		await vi.waitFor(() => expect(rowFor('starting-up').textContent).toContain(NOT_LOADED), { timeout: 3000 });
		expect(document.querySelector('.section.loading')).toBeNull();
	});
});
