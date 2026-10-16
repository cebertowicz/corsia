import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/version', async () => {
	const actual = await vi.importActual<typeof import('$lib/version')>('$lib/version');
	return { ...actual, loadServerVersion: vi.fn(async () => {}) };
});

import VersionOverlay from './VersionOverlay.svelte';
import { coreSettings, type CoreSettings } from '$lib/stores/settings';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const withFeatures = (features: CoreSettings['features']): void => {
	coreSettings.set(features === undefined ? {} : { features });
};

beforeEach(() => {
	host = document.createElement('div');
	document.body.append(host);
});

afterEach(() => {
	component ? unmount(component) : null;
	component = null;
	coreSettings.set(null);
	host.remove();
});

const stamp = (): Element | null => host.querySelector('.version-overlay');

const render = (): void => {
	component = mount(VersionOverlay, { target: host }) as Record<string, unknown>;
	flushSync();
};

describe('VersionOverlay', () => {
	it('renders nothing when the debug overlay feature is off', () => {
		withFeatures({ debug_version_overlay: false });
		render();
		expect(stamp()).toBeNull();
	});

	it('renders the build stamp when the feature is on', () => {
		withFeatures({ debug_version_overlay: true });
		render();
		expect(stamp()).not.toBeNull();
	});

	it('does not render a build stamp before the backend resolves settings', () => {
		withFeatures(undefined);
		render();
		expect(stamp()).toBeNull();
	});
});
