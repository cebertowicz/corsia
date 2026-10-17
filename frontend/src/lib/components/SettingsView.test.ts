import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SettingsView from './SettingsView.svelte';
import { token } from '$lib/stores/auth';
import { SETTINGS_SECTIONS, closeSettingsProject, openProjectSettings, requestSettingsSection, resetSettingsNavigation, selectSettingsSection, settingsNavigation } from '$lib/stores/settingsNavigation';
import { get } from 'svelte/store';

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
	resetSettingsNavigation();
});

describe('SettingsView navigation', () => {
	it('renders a nav entry and a section for every SETTINGS_SECTIONS id', () => {
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		const entries = [...host.querySelectorAll('.desktop-nav .nav-item')].map((el) => el.getAttribute('data-settings-section'));
		expect(entries).toEqual([...SETTINGS_SECTIONS]);
		for (const id of SETTINGS_SECTIONS) {
			selectSettingsSection(id);
			flushSync();
			expect(host.querySelector('.settings-content')?.children.length, id).toBeGreaterThan(0);
		}
	});

	it('draws the Connected apps title as the page title, as Skills does', () => {
		selectSettingsSection('connected-apps');
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		const titles = [...host.querySelectorAll('.section-host > h2.page-title')].map((el) => el.textContent);
		expect(titles).toEqual(['Connected apps']);
		expect(host.querySelectorAll('.section-host h2')).toHaveLength(1);
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
		selectSettingsSection('debug');

		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		expect(host.textContent).toContain('Debug');
	});

	it('opens a gear request made before Settings mounts to its page, not the phone list', () => {
		openProjectSettings('p-garden');

		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		expect(host.querySelector('.settings-content')?.classList.contains('mobile-section')).toBe(true);
		expect(get(settingsNavigation).drill?.backLabel).toBe('Projects');
		closeSettingsProject();
	});

	it('shows the phone list when Settings opens without a new request', () => {
		openProjectSettings('p-garden');
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();
		unmount(component);
		component = null;
		closeSettingsProject();

		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();

		expect(host.querySelector('.settings-content')?.classList.contains('mobile-section')).toBe(false);
	});

	it('mounts a phone section only once it is open, so an anchor scrolls in shown content', () => {
		vi.stubGlobal('matchMedia', (query: string) => ({ matches: true, media: query, addEventListener: () => {}, removeEventListener: () => {} }));
		const shownWhenScrolled: boolean[] = [];
		Element.prototype.scrollIntoView = function (this: Element) {
			shownWhenScrolled.push(!!this.closest('.settings-content')?.classList.contains('mobile-section'));
		};
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();
		expect(host.querySelector('.section-host')).toBeNull();
		unmount(component);
		component = null;

		requestSettingsSection('system', 'update');
		component = mount(SettingsView, { target: host, props: { systemStatus: {} } });
		flushSync();
		expect(shownWhenScrolled).toEqual([true]);
	});
});
