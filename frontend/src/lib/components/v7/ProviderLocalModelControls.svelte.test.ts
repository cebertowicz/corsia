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
		component = mount(ProviderLocalModelControls, { target: host, props: { onAdd: vi.fn(), onAddProvider, corsiaManaged: true } });
		await tick(); await tick(); flushSync();

		expect(host.textContent).toContain('Adding a local model through CORSIA needs at least 32 GB of memory. Smaller models and future updates may bring local models to computers with less memory. Connect a subscription or an API provider.');
		expect([...host.querySelectorAll('button')].map((button) => button.textContent?.trim())).toEqual(['Add provider']);
		expect(host.querySelector('button')?.classList.contains('ghost')).toBe(true);
		expect(host.textContent).not.toContain('Add model');
		host.querySelector<HTMLButtonElement>('button')!.click();
		expect(onAddProvider).toHaveBeenCalledOnce();
	});

	it('shows no low-memory refusal on a host that can never run a local model', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'ready', eligible: false, never_local: true }] }) })));
		component = mount(ProviderLocalModelControls, { target: host, props: { onAdd: vi.fn(), onAddProvider: vi.fn(), corsiaManaged: true } });
		await tick(); await tick(); flushSync();

		expect(host.textContent).not.toContain('24 GB');
		expect(host.textContent).not.toContain('Add model');
	});

	it('offers no add action for a person-owned oMLX card', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'ready', eligible: true }] }) })));
		component = mount(ProviderLocalModelControls, { target: host, props: { onAdd: vi.fn(), onAddProvider: vi.fn() } });
		await tick(); await tick(); flushSync();

		expect(host.querySelector('button')).toBeNull();
	});
});
