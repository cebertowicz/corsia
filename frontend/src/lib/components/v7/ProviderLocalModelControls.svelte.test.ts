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
		component = mount(ProviderLocalModelControls, { target: host, props: { onStarted: vi.fn(), onAddProvider } });
		await tick(); await tick(); flushSync();

		expect(host.textContent).toContain('This computer doesn’t have enough memory to run a local model, so pick at least one above.');
		expect(host.textContent).toContain('Connect a subscription or an API key instead.');
		expect([...host.querySelectorAll('button')].map((button) => button.textContent?.trim())).toEqual(['Add provider']);
		expect(host.textContent).not.toContain('Add model');
		host.querySelector<HTMLButtonElement>('button')!.click();
		expect(onAddProvider).toHaveBeenCalledOnce();
	});
});
