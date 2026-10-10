import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ProvidersV7View from './ProvidersV7View.svelte';
import { groupByHost, hostGroupSnapshot, type HostGroup, type HostProvider } from '$lib/stores/providersV7';
import { providerCardRequest } from '$lib/stores/settingsNavigation';
import { vendorAccounts } from '$lib/stores/vendorAccounts';

const harness = (name: string, label: string): HostProvider => ({
	name, label, type: 'harness', statusDot: 'online', subline: 'Signed in', raw: { name, label } as HostProvider['raw'],
});

const originalScroll = Element.prototype.scrollIntoView;

describe('ProvidersV7View card request', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		vi.unstubAllGlobals();
		Element.prototype.scrollIntoView = originalScroll;
		hostGroupSnapshot.set([]);
		providerCardRequest.set(null);
		vendorAccounts.set([]);
	});

	it('opens the requested provider card without changing account settings', async () => {
		const fetchMock = vi.fn(() => new Promise<never>(() => {}));
		vi.stubGlobal('fetch', fetchMock);
		const scrolled = vi.fn();
		Element.prototype.scrollIntoView = scrolled;
		hostGroupSnapshot.set([{
			key: 'cloud', name: 'Cloud', subname: '', netKind: 'cloud',
			providers: [harness('claude-code', 'Claude Code'), harness('codex', 'Codex')],
		} as HostGroup]);
		providerCardRequest.set('codex');
		// The switch belongs to an account, so the card shows it once the account list is known.
		vendorAccounts.set([{
			id: 'codex-default', provider: 'codex', name: 'Default', is_default: true, state: 'legacy', revision: 0,
			identity: {}, label: { name: 'Plus account 1', email: null, text: 'Plus account 1' }, allow_overage: false,
		}]);

		component = mount(ProvidersV7View, { target: document.body });
		flushSync();
		await tick();

		const codex = document.querySelector('[data-provider-card="codex"]');
		const claude = document.querySelector('[data-provider-card="claude-code"]');
		expect(codex?.classList.contains('expanded')).toBe(true);
		expect(codex?.textContent).toContain('Loading models…');
		expect(claude?.classList.contains('expanded')).toBe(false);
		expect(scrolled).toHaveBeenCalledOnce();
		expect(get(providerCardRequest)).toBeNull();
		const methods = fetchMock.mock.calls.map((call) => (call as unknown[])[1] as RequestInit | undefined);
		expect(methods.every((init) => (init?.method ?? 'GET') === 'GET')).toBe(true);
	});

	it('renders a keyed cloud provider that is switched off with the off dot, off subline and off switch', async () => {
		vi.stubGlobal('fetch', vi.fn(() => new Promise<never>(() => {})));
		const cloud = (name: string, label: string, enabled: boolean) => ({
			name, label, type: 'cloud', has_key: true, enabled, masked_key: null, api_base: null,
			docs_url: null, models: ['a', 'b'],
		}) as HostProvider['raw'];
		hostGroupSnapshot.set(groupByHost([cloud('anthropic', 'Anthropic', false), cloud('openai', 'OpenAI', true)], [], null));

		component = mount(ProvidersV7View, { target: document.body });
		flushSync();
		await tick();

		const read = (name: string) => {
			const card = document.querySelector(`[data-provider-card="${name}"]`);
			return [
				card?.querySelector('.dot')?.classList.contains('disabled'),
				card?.querySelector('.meta')?.textContent,
				card?.querySelector('.tg')?.classList.contains('on'),
			];
		};
		expect(read('anthropic')).toEqual([true, 'off · 2 models', false]);
		expect(read('openai')).toEqual([false, '2 models', true]);
	});
});
