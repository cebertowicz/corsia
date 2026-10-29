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
	it('offers the low-memory provider path when no local model is ready', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		const onAddProvider = vi.fn();
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', eligible: false }] }) })));
		component = mount(ProviderLocalModelControls, { target: host, props: { onStarted: vi.fn(), onAddProvider, corsiaManaged: true } });
		await tick(); await tick(); flushSync();

		expect(host.textContent).toContain('Local models need at least 32 GB of memory, and this computer has less. Smaller models and future updates may bring them to computers with less memory. Connect a subscription or an API provider.');
		expect([...host.querySelectorAll('button')].map((button) => button.textContent?.trim())).toEqual(['Add provider']);
		expect(host.querySelector('button')?.classList.contains('ghost')).toBe(true);
		expect(host.textContent).not.toContain('Add model');
		host.querySelector<HTMLButtonElement>('button')!.click();
		expect(onAddProvider).toHaveBeenCalledOnce();
	});

	it('hides the low-memory refusal and Add model when an existing local model is ready', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'ready', eligible: false }] }) })));
		component = mount(ProviderLocalModelControls, { target: host, props: { onStarted: vi.fn(), onAddProvider: vi.fn(), corsiaManaged: true } });
		await tick(); await tick(); flushSync();

		expect(host.textContent).not.toContain('Local models need at least 32 GB of memory');
		expect(host.textContent).not.toContain('Add model');
	});

	it('shows no low-memory refusal on a host that can never run a local model', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'ready', eligible: false, never_local: true }] }) })));
		component = mount(ProviderLocalModelControls, { target: host, props: { onStarted: vi.fn(), onAddProvider: vi.fn(), corsiaManaged: true } });
		await tick(); await tick(); flushSync();

		expect(host.textContent).not.toContain('Local models need at least 32 GB of memory');
		expect(host.textContent).not.toContain('Add model');
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
