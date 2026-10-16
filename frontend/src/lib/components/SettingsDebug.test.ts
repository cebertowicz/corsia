import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SettingsDebug from './SettingsDebug.svelte';
import { token } from '$lib/stores/auth';
import { coreSettings } from '$lib/stores/settings';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function toggleInCard(title: string): HTMLInputElement {
	const card = [...host.querySelectorAll('.settings-card')].find(
		(el) => el.querySelector('h3')?.textContent === title,
	);
	const input = card?.querySelector('input[type="checkbox"]');
	if (!(input instanceof HTMLInputElement)) throw new Error(`${title} toggle not rendered`);
	return input;
}

function profilerToggle(): HTMLInputElement {
	return toggleInCard('CPU profiler');
}

function overlayToggle(): HTMLInputElement {
	return toggleInCard('On-screen overlay');
}

beforeEach(() => {
	token.set('test-token');
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
	coreSettings.set(null);
});

describe('SettingsDebug overlay toggle', () => {
	it('waits for the backend feature truth instead of inventing an on default', async () => {
		coreSettings.set({ features: {} } as never);
		component = mount(SettingsDebug, { target: host });
		flushSync();
		expect(overlayToggle().checked).toBe(false);

		coreSettings.set({ features: { debug_version_overlay: true } } as never);
		await tick();
		expect(overlayToggle().checked).toBe(true);
	});
});

describe('SettingsDebug CPU profiler toggle', () => {
	it('renders the cost copy and reflects the saved flag', async () => {
		coreSettings.set({ features: { cpu_profiler: false } } as never);
		component = mount(SettingsDebug, { target: host });
		flushSync();
		expect(host.textContent).toContain(
			'Samples CPU use ten times a second and logs a summary each minute. Leave off unless diagnosing slowness.',
		);
		expect(profilerToggle().checked).toBe(false);

		coreSettings.set({ features: { cpu_profiler: true } } as never);
		await tick();
		expect(profilerToggle().checked).toBe(true);
	});

	it('saves cpu_profiler through updateSettings when toggled', async () => {
		coreSettings.set({ features: { cpu_profiler: false } } as never);
		const fetchMock = vi.fn(async () => ({
			ok: true,
			json: async () => ({ ok: true, features: { cpu_profiler: true } }),
		}));
		vi.stubGlobal('fetch', fetchMock);
		component = mount(SettingsDebug, { target: host });
		flushSync();

		const toggle = profilerToggle();
		toggle.checked = true;
		toggle.dispatchEvent(new Event('change', { bubbles: true }));
		await vi.waitFor(() => {
			const patch = fetchMock.mock.calls.find(
				([, init]) => (init as RequestInit | undefined)?.method === 'PATCH',
			);
			expect(patch).toBeTruthy();
			expect(JSON.parse((patch![1] as RequestInit).body as string)).toEqual({
				features: { cpu_profiler: true },
			});
		});
	});
});
