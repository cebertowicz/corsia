import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SystemBulletinsSetting from './SystemBulletinsSetting.svelte';
import { token } from '$lib/stores/auth';
import { coreSettings, loadSettings, settingsLoadError, settingsLoading } from '$lib/stores/settings';

const TOGGLE = 'button[aria-label="Check for maintainer bulletins"]';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function toggle() {
	return host.querySelector<HTMLButtonElement>(TOGGLE)!;
}

function render() {
	component = mount(SystemBulletinsSetting, { target: host });
	flushSync();
}

function stubSettingsApi(patchOk: boolean, readback: (() => unknown) | null) {
	const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
		if (init?.method === 'PATCH') {
			// Like the backend, a successful PATCH replies with the stored value.
			const sent = JSON.parse(String(init.body)).live_bulletins;
			return patchOk
				? { ok: true, json: async () => ({ live_bulletins: { enabled: sent.enabled } }) }
				: { ok: false, json: async () => ({ detail: 'Bulletin preference could not be saved.' }) };
		}
		return readback ? { ok: true, json: async () => readback() } : { ok: false, json: async () => ({}) };
	});
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
}

beforeEach(() => {
	token.set('test-token');
	coreSettings.set({ live_bulletins: { enabled: true } });
	settingsLoading.set(false);
	settingsLoadError.set(false);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	token.set(null);
	coreSettings.set(null);
	settingsLoading.set(false);
	settingsLoadError.set(false);
});

describe('SystemBulletinsSetting', () => {
	it('shows only the value the server reports, never an invented default', () => {
		coreSettings.set({});
		render();

		expect(toggle().disabled).toBe(true);
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
		expect(host.textContent).toContain('System bulletins');
		expect(host.textContent).toContain('Once a day, CORSIA checks for signed notes from its maintainers about how its agents work.');

		coreSettings.set({ live_bulletins: { enabled: true } });
		flushSync();
		expect(toggle().disabled).toBe(false);
		expect(toggle().getAttribute('aria-pressed')).toBe('true');

		coreSettings.set({ live_bulletins: { enabled: false } });
		flushSync();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
	});

	it('disables the switch while the first read is pending, then shows a stored false', () => {
		coreSettings.set(null);
		settingsLoading.set(true);
		render();

		expect(toggle().disabled).toBe(true);
		expect(host.querySelector('[role="status"]')?.textContent).toContain('Loading saved setting');

		coreSettings.set({ live_bulletins: { enabled: false } });
		settingsLoading.set(false);
		flushSync();
		expect(toggle().disabled).toBe(false);
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
		expect(host.querySelector('[role="status"]')).toBeNull();
	});

	it('offers a retry when the first read fails, and shows the value the retry reads', async () => {
		coreSettings.set(null);
		settingsLoadError.set(true);
		stubSettingsApi(true, () => ({ live_bulletins: { enabled: false } }));
		render();

		expect(toggle().disabled).toBe(true);
		const alert = host.querySelector('[role="alert"]');
		expect(alert?.textContent).toContain('Could not read this setting.');
		alert!.querySelector('button')!.click();

		await vi.waitFor(() => {
			flushSync();
			expect(toggle().disabled).toBe(false);
			expect(toggle().getAttribute('aria-pressed')).toBe('false');
		});
		expect(host.querySelector('[role="alert"]')).toBeNull();
	});

	it('persists the choice without sending a base URL', async () => {
		const fetchMock = stubSettingsApi(true, () => ({ live_bulletins: { enabled: false } }));
		render();
		toggle().click();

		await vi.waitFor(() => {
			const patch = fetchMock.mock.calls.find((call) => (call[1] as RequestInit | undefined)?.method === 'PATCH');
			expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({ live_bulletins: { enabled: false } });
		});
		await vi.waitFor(() => {
			flushSync();
			expect(toggle().getAttribute('aria-pressed')).toBe('false');
		});
		expect(host.querySelector('[role="alert"]')).toBeNull();
	});

	it('shows the save error as its own line when saving fails', async () => {
		stubSettingsApi(false, () => ({ live_bulletins: { enabled: true } }));
		render();
		toggle().click();

		await vi.waitFor(() => {
			expect(host.querySelector('.error')?.textContent).toContain('Bulletin preference could not be saved.');
		});
		expect(toggle().getAttribute('aria-pressed')).toBe('true');
	});

	for (const [from, to] of [[true, false], [false, true]] as const) {
		it(`shows the saved ${to} from the PATCH reply when the refresh after it fails`, async () => {
			coreSettings.set({ live_bulletins: { enabled: from } });
			stubSettingsApi(true, null);
			render();
			toggle().click();

			await vi.waitFor(() => {
				flushSync();
				expect(host.querySelector('[role="alert"]')?.textContent).toContain('Could not refresh this setting.');
			});
			expect(toggle().getAttribute('aria-pressed')).toBe(String(to));
			expect(toggle().disabled).toBe(false);
			expect(host.querySelector('.error')).toBeNull();
		});
	}

	it('keeps the last confirmed value when the PATCH reply has no boolean and the refresh fails', async () => {
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
			init?.method === 'PATCH' ? { ok: true, json: async () => ({}) } : { ok: false, json: async () => ({}) }));
		render();
		toggle().click();

		await vi.waitFor(() => {
			flushSync();
			expect(host.querySelector('[role="alert"]')?.textContent).toContain('Could not refresh this setting.');
		});
		expect(toggle().getAttribute('aria-pressed')).toBe('true');
	});

	it('disables the switch while saving and settles on the second of two saves', async () => {
		let release: () => void = () => {};
		stubSettingsApi(true, () => ({ live_bulletins: { enabled: false } }));
		const settle = vi.mocked(fetch);
		settle.mockImplementationOnce(async (_input, init) => {
			await new Promise<void>((resolve) => { release = resolve; });
			return { ok: true, json: async () => JSON.parse(String(init!.body)) } as Response;
		});
		render();
		toggle().click();
		flushSync();
		expect(toggle().disabled).toBe(true);
		release();

		await vi.waitFor(() => {
			flushSync();
			expect(toggle().disabled).toBe(false);
			expect(toggle().getAttribute('aria-pressed')).toBe('false');
		});
		stubSettingsApi(true, () => ({ live_bulletins: { enabled: true } }));
		toggle().click();
		await vi.waitFor(() => {
			flushSync();
			expect(toggle().getAttribute('aria-pressed')).toBe('true');
		});
	});

	it('ignores a settings read that started before the save and finished after it', async () => {
		let releaseStale: () => void = () => {};
		let reads = 0;
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PATCH') return { ok: true, json: async () => ({ live_bulletins: { enabled: false } }) };
			if (++reads === 1) {
				await new Promise<void>((resolve) => { releaseStale = resolve; });
				return { ok: true, json: async () => ({ live_bulletins: { enabled: true } }) };
			}
			return { ok: true, json: async () => ({ live_bulletins: { enabled: false } }) };
		}));
		render();
		const staleRead = loadSettings();
		toggle().click();
		await vi.waitFor(() => {
			flushSync();
			expect(reads).toBe(2);
			expect(toggle().getAttribute('aria-pressed')).toBe('false');
		});
		releaseStale();
		await staleRead;
		flushSync();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
	});
});
