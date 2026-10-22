import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ProviderLocalModelControls from './ProviderLocalModelControls.svelte';
import { token } from '$lib/stores/auth';

let component: ReturnType<typeof mount> | null = null;
let host: HTMLElement;

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host?.remove();
	vi.unstubAllGlobals();
	token.set(null);
});

describe('ProviderLocalModelControls', () => {
	it('uses the source eligibility gate to replace Add model with the low-memory provider path', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		const onAddProvider = vi.fn();
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'ready', eligible: false }] }) })));
		component = mount(ProviderLocalModelControls, { target: host, props: { onStarted: vi.fn(), onAddProvider, corsiaManaged: true } });
		await tick(); await tick(); flushSync();

		expect(host.textContent).toContain('Local models need at least 24 GB of memory, and this computer has less. Smaller models and future updates may bring them to 16 GB computers. Connect a subscription or an API provider.');
		expect([...host.querySelectorAll('button')].map((button) => button.textContent?.trim())).toEqual(['Add provider']);
		expect(host.querySelector('button')?.classList.contains('secondary')).toBe(true);
		expect(host.textContent).not.toContain('Add model');
		host.querySelector<HTMLButtonElement>('button')!.click();
		expect(onAddProvider).toHaveBeenCalledOnce();
	});

	it('offers no add action for a person-owned oMLX card', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'ready', eligible: true }] }) })));
		component = mount(ProviderLocalModelControls, { target: host, props: { onStarted: vi.fn(), onAddProvider: vi.fn() } });
		await tick(); await tick(); flushSync();

		expect(host.querySelector('button')).toBeNull();
	});
});
