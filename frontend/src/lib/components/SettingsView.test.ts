import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SettingsView from './SettingsView.svelte';
import { token } from '$lib/stores/auth';
import { SETTINGS_SECTIONS, settingsActiveSection } from '$lib/stores/settingsNavigation';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	token.set('test-token');
	vi.stubGlobal('fetch', vi.fn(async () => ({
		ok: true,
		json: async () => ({ definitions: [], instances: [], providers: [], models: [] }),
	})));
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
	settingsActiveSection.set('providers');
});

describe('SettingsView navigation', () => {
	it('renders a nav entry and a section for every SETTINGS_SECTIONS id', () => {
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		const entries = [...host.querySelectorAll('.desktop-nav .nav-item')].map((el) => el.textContent?.trim().toLowerCase());
		expect(entries).toEqual([...SETTINGS_SECTIONS]);
		for (const id of SETTINGS_SECTIONS) {
			settingsActiveSection.set(id);
			flushSync();
			expect(host.querySelector('.settings-content')?.children.length, id).toBeGreaterThan(0);
		}
	});

	it('keeps Settings navigation limited to live product surfaces', async () => {
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		expect(host.textContent).toContain('Providers');
		// PL-7: Measurement has one home in the rail, so Settings must not list it.
		expect(host.textContent).not.toContain('Measurement');
		expect(host.textContent).not.toContain('Costs');
		expect(host.textContent).toContain('System');
		expect(host.textContent).toContain('Debug');
	});

	it('opens directly to a requested settings section', async () => {
		settingsActiveSection.set('debug');

		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		expect(host.textContent).toContain('Debug');
	});
});
