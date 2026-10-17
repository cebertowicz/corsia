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
	it('renders Connected apps as its own desktop section and mobile Apps tab', () => {
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		const desktopEntries = [...host.querySelectorAll('.desktop-nav .nav-item')].map((el) => el.textContent?.trim());
		const mobileEntries = [...host.querySelectorAll('.mobile-tab')].map((el) => el.textContent?.trim());
		expect(desktopEntries).toEqual(['Providers', 'Connected apps', 'System', 'Keys', 'Debug']);
		expect(mobileEntries).toEqual(['Providers', 'Apps', 'System', 'Keys', 'Debug']);
		for (const id of SETTINGS_SECTIONS) {
			settingsActiveSection.set(id);
			flushSync();
			expect(host.querySelector('.settings-content')?.children.length, id).toBeGreaterThan(0);
		}
	});

	it('renders Connected apps only in its own section', () => {
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });

		settingsActiveSection.set('system');
		flushSync();
		expect(host.querySelector('.settings-content')?.textContent).not.toContain('No connected apps yet');

		settingsActiveSection.set('connected-apps');
		flushSync();
		expect(host.querySelector('.settings-content')?.textContent).toContain('Connected apps');
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
