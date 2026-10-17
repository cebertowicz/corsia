import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsProjectContext from './SettingsProjectContext.svelte';
import { token } from '$lib/stores/auth';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	vi.unstubAllGlobals();
});

describe('SettingsProjectContext', () => {
	it('stops loading when there is no token to load with', () => {
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);
		token.set(null);
		component = mount(SettingsProjectContext, { target: host, props: { projectId: 'atlas' } });
		flushSync();

		expect(host.textContent).not.toContain('Loading');
		expect(host.textContent).toContain('No context files for this project yet.');
		expect(fetchMock).not.toHaveBeenCalled();
	});
});
