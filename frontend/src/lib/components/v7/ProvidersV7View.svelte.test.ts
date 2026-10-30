import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ProvidersV7View from './ProvidersV7View.svelte';
import { groupByHost, hostGroupSnapshot, type HostGroup, type HostProvider } from '$lib/stores/providersV7';
import { providerCardRequest, requestSettingsSection, resetSettingsNavigation } from '$lib/stores/settingsNavigation';
import { vendorAccounts } from '$lib/stores/vendorAccounts';
import { token } from '$lib/stores/auth';
import { toasts } from '$lib/stores/toast';
import { LOW_MEMORY_LOCAL_MODEL_SENTENCE } from '$lib/setupLocalPick.svelte';
import { emit } from '$lib/ws/events';

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
		for (const toast of get(toasts)) toasts.dismiss(toast.id);
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
			identity: {}, label: { name: 'Plus account 1', email: null, text: 'Plus account 1', custom: null }, allow_overage: false,
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

	it('opens the settings of the provider that holds the account a link names, and the account row takes the landing', async () => {
		const row = (id: string, provider: string, name: string, state: 'signed_in' | 'signed_out') => ({
			id, provider, name, is_default: false, state, revision: 1, identity: {}, label: { name, email: null, text: name, custom: name },
		});
		const accounts = [row('claude-1', 'claude-code', 'FOMRRT1', 'signed_out'), row('codex-1', 'codex', 'FOMRRT1', 'signed_in')];
		vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
			ok: true, status: 200, json: async () => String(url).includes('/api/vendor-accounts') ? { accounts } : { providers: [], servers: [], models: [], sources: [] },
		})));
		Element.prototype.scrollIntoView = vi.fn();
		hostGroupSnapshot.set([{
			key: 'cloud', name: 'Cloud', subname: '', netKind: 'cloud',
			providers: [harness('claude-code', 'Claude Code'), harness('codex', 'Codex')],
		} as HostGroup]);
		vendorAccounts.set(accounts);
		requestSettingsSection('providers', 'account-claude-1');

		component = mount(ProvidersV7View, { target: document.body });
		flushSync();
		await vi.waitFor(() => expect(document.querySelector('#vendor-account-claude-1')).not.toBeNull());
		expect(document.querySelector('#vendor-account-codex-1')).toBeNull();
		await vi.waitFor(() => expect(document.activeElement?.closest('#vendor-account-claude-1')).not.toBeNull());
		expect(document.activeElement?.textContent?.trim()).toBe('Sign in');
		resetSettingsNavigation();
	});

	it('opens the requested server card when the request names a server id', async () => {
		vi.stubGlobal('fetch', vi.fn(() => new Promise<never>(() => {})));
		const scrolled = vi.fn();
		Element.prototype.scrollIntoView = scrolled;
		const onServer = (id: string): HostProvider => ({
			name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: id,
			raw: { name: 'omlx', label: 'oMLX' } as HostProvider['raw'],
			server: { id, provider: 'omlx', kind: 'managed', instance_name: id, endpoints: [] } as unknown as HostProvider['server'],
		});
		hostGroupSnapshot.set([{
			key: 'lan', name: 'LAN', subname: '', netKind: 'lan',
			providers: [onServer('ns-1'), onServer('ns-2')],
		} as HostGroup]);
		providerCardRequest.set('ns-2');

		component = mount(ProvidersV7View, { target: document.body });
		flushSync();
		await tick();

		expect(document.querySelector('[data-server-card="ns-2"]')?.classList.contains('expanded')).toBe(true);
		expect(document.querySelector('[data-server-card="ns-1"]')?.classList.contains('expanded')).toBe(false);
		expect(scrolled).toHaveBeenCalledOnce();
	});

	it('shows the local setup row only when CORSIA has no writable local oMLX', async () => {
		const studio: HostProvider = {
			name: 'omlx', label: 'oMLX', type: 'network', statusDot: 'online', subline: 'port 11434',
			raw: { name: 'omlx', label: 'oMLX' } as HostProvider['raw'],
			server: { id: 'studio', provider: 'omlx', instance_name: 'Studio', endpoints: [] } as unknown as HostProvider['server'],
		};
		const setupBodies: object[] = [];
		const render = async (source: object, servers: object[] = []) => {
			token.set('test-token');
			vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
				if (String(url).includes('/providers/sources')) return { ok: true, json: async () => ({ sources: [source] }) };
				if (String(url).includes('/providers/local/setup')) {
					const body = JSON.parse(String(init?.body ?? '{}'));
					setupBodies.push(body);
					return { ok: true, status: 200, json: async () => ({ ok: true }) };
				}
				return { ok: true, json: async () => ({ providers: [], servers, accounts: [], models: [] }) };
			}));
			hostGroupSnapshot.set([{ key: 'lan', name: 'Studio', subname: '', netKind: 'lan', providers: [studio] } as HostGroup]);
			component = mount(ProvidersV7View, { target: document.body });
			flushSync();
			await tick(); await tick();
		};

		await render({ source: 'omlx', eligible: true, corsia_omlx_online: false });
		expect(document.querySelector('[data-provider-card="local-setup"]')?.textContent).toContain('Set up');
		expect(document.querySelector('[data-provider-card="local-setup"] .dot')?.classList.contains('disabled')).toBe(true);
		expect(document.querySelector('[data-provider-card="local-setup"] [aria-label="Settings"]')).toBeNull();
		expect(document.querySelector('[data-provider-card="local-setup"] button.tg')).toBeNull();
		expect(document.querySelector('[data-provider-card="local-setup"] button.pr-expand')).toBeNull();
		expect(document.querySelector('[data-provider-card="local-setup"] .chev')).toBeNull();
		(document.querySelector('[data-provider-card="local-setup"] .setup-action') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(setupBodies.at(-1)).toEqual({}));
		expect(get(toasts)).toHaveLength(0);
		if (component) unmount(component);
		component = null; document.body.innerHTML = '';

		await render({ source: 'omlx', eligible: false, corsia_omlx_online: false });
		expect(document.querySelector('[data-provider-card="local-setup"]')?.textContent).toContain(LOW_MEMORY_LOCAL_MODEL_SENTENCE);
		if (component) unmount(component);
		component = null; document.body.innerHTML = '';

		await render({ source: 'omlx', eligible: true, corsia_omlx: true, corsia_omlx_online: true });
		expect(document.querySelector('[data-provider-card="local-setup"]')).toBeNull();
		if (component) unmount(component);
		component = null; document.body.innerHTML = '';

		// A server that is not macOS lists no local setup at all, whatever its memory.
		for (const eligible of [true, false]) {
			await render({ source: 'omlx', eligible, never_local: true, corsia_omlx_online: false });
			expect(document.querySelector('[data-provider-card="local-setup"]')).toBeNull();
			expect(document.body.textContent).not.toContain(LOW_MEMORY_LOCAL_MODEL_SENTENCE);
			if (component) unmount(component);
			component = null; document.body.innerHTML = '';
		}

		// CORSIA's own oMLX registered but offline is still the local model: one row, no Set up beside it.
		const ownOffline = {
			id: 'corsia', provider: 'omlx', instance_name: 'Local model', status: 'offline', corsia_omlx: true,
			endpoints: [{ host: '127.0.0.1', port: 8210 }],
		};
		await render({ source: 'omlx', eligible: true, corsia_omlx: true, corsia_omlx_online: false }, [ownOffline]);
		await vi.waitFor(() => expect(document.querySelector('[data-server-card="corsia"]')).not.toBeNull());
		expect(document.querySelector('[data-provider-card="local-setup"]')).toBeNull();
	});

	it('shows a failed setup with its fix and Retry, and a running one without a button', async () => {
		const render = async (source: object) => {
			token.set('test-token');
			vi.stubGlobal('fetch', vi.fn(async (url: string) => {
				if (String(url).includes('/providers/sources')) return { ok: true, json: async () => ({ sources: [source] }) };
				return { ok: true, json: async () => ({ providers: [], servers: [], accounts: [], models: [] }) };
			}));
			component = mount(ProvidersV7View, { target: document.body });
			flushSync();
			await tick(); await tick();
		};
		await render({ source: 'omlx', eligible: true, corsia_omlx_online: false, local_state: 'setup_failed', setup_kind: 'setup', fix: 'Run `brew upgrade jundot/omlx/omlx`, then press Retry.' });
		const row = () => document.querySelector('[data-provider-card="local-setup"]');
		expect(row()?.textContent).toContain('brew upgrade jundot/omlx/omlx');
		expect(row()?.querySelector('.setup-action')?.textContent).toBe('Retry');
		expect(row()?.querySelector('.dot')?.classList.contains('error')).toBe(true);
		if (component) unmount(component);
		component = null; document.body.innerHTML = '';

		await render({ source: 'omlx', eligible: true, corsia_omlx_online: false, local_state: 'downloading', setup_kind: 'setup', settings_download: { name: 'DeepSeek V4 Flash 0731', repository: 'x/d', percent: 12, failed: false } });
		expect(row()?.textContent).toContain('Downloading DeepSeek V4 Flash 0731 · 12%');
		expect(row()?.querySelector('.setup-action')).toBeNull();
		expect(row()?.textContent).toContain('Setting up…');
	});

	it('reloads the host groups once CORSIA\'s oMLX comes online during setup', async () => {
		let online = false;
		const serverReads = () => fetchMock.mock.calls.filter((call) => String((call as unknown[])[0]).includes('/settings/network-servers')).length;
		const fetchMock = vi.fn(async (url: string) => {
			if (String(url).includes('/providers/sources')) {
				return { ok: true, json: async () => ({ sources: [{ source: 'omlx', eligible: true, corsia_omlx: online, corsia_omlx_online: online }] }) };
			}
			return { ok: true, json: async () => ({ providers: [], servers: [], accounts: [], models: [] }) };
		});
		token.set('test-token');
		vi.stubGlobal('fetch', fetchMock);
		component = mount(ProvidersV7View, { target: document.body });
		flushSync();
		await tick(); await tick();
		expect(document.querySelector('[data-provider-card="local-setup"]')).not.toBeNull();

		await new Promise((resolve) => setTimeout(resolve, 100));
		const beforeOnline = serverReads();

		online = true;
		// The listener registers once mount's reads finish, so keep announcing until the row reacts.
		await vi.waitFor(() => {
			emit('models_updated', {});
			expect(document.querySelector('[data-provider-card="local-setup"]')).toBeNull();
		});
		expect(serverReads()).toBeGreaterThan(beforeOnline);
	});

	it('offers the setup flow\'s own row for a CLI without a card, and starts its install from Sign in', async () => {
		token.set('test-token');
		const sources = [
			{ source: 'claude-code', name: 'Claude Code', state: 'needs_auth' },
			{ source: 'codex', name: 'Codex', state: 'not_installed' },
		];
		const claudeModel = { id: 'claude-1', harness: 'claude-code', provider: 'claude-code', enabled: 0, available: true };
		const fetchMock = vi.fn(async (url: string) => String(url).includes('/providers/sources')
			? { ok: true, json: async () => ({ sources }) }
			: { ok: true, json: async () => ({ providers: [], servers: [], accounts: [], models: [claudeModel] }) });
		vi.stubGlobal('fetch', fetchMock);
		// Claude Code already has a card; only Codex needs the row.
		hostGroupSnapshot.set([{
			key: 'agent-clis', name: 'Agent CLIs', subname: '', netKind: 'localhost', providers: [harness('claude-code', 'Claude Code')],
		} as HostGroup]);

		component = mount(ProvidersV7View, { target: document.body });
		flushSync();
		// No row until the first host-group read says which tools already have a card.
		expect(document.querySelector('section[aria-label="Codex"]')).toBeNull();
		await vi.waitFor(() => expect(document.querySelector('section[aria-label="Codex"]')).not.toBeNull());

		expect(document.querySelector('[data-provider-card="claude-code"]')).not.toBeNull();
		expect(document.querySelector('section[aria-label="Claude Code"]')).toBeNull();
		const row = document.querySelector('section[aria-label="Codex"]');
		expect(row?.textContent).toContain('Uses your ChatGPT subscription.');
		const signIn = [...(row?.querySelectorAll('button') ?? [])].find((button) => button.textContent?.includes('Sign in')) as HTMLButtonElement;
		signIn.click();
		await vi.waitFor(() => expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith('/providers/cli/codex/install'))).toBe(true));
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
