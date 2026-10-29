import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { writable } from 'svelte/store';

const removeNetworkServer = vi.fn(async (_id: string) => ({ ok: true }));
vi.mock('$lib/stores/networkServers', () => ({
	removeNetworkServer: (id: string) => removeNetworkServer(id),
	testNetworkServer: vi.fn(),
}));
vi.mock('$lib/stores/providersV7', () => ({
	hostGroups: writable([]),
	loadHostGroups: vi.fn(async () => {}),
}));

import ProviderSettingsModal from './ProviderSettingsModal.svelte';
import { resetSettingsNavigation } from '$lib/stores/settingsNavigation';

const server = {
	id: 'ns-plan', provider: 'external', instance_id: 'token-plan.example/v1',
	instance_name: 'Token Plan', kind: 'external' as const, dialect: 'openai' as const,
	port: 443, status: 'online' as const, source: 'manual' as const,
	latency_ms: null, memory_total_mb: null, memory_used_mb: null, gpu_info: null,
	endpoints: [], active_host: 'token-plan.example', api_base: null, mgmt_base: null,
	has_key: true, metadata: { base_url: 'https://token-plan.example/v1' },
};
const provider = {
	name: 'external', label: 'OpenAI-compatible API', type: 'cloud-endpoint',
	statusDot: 'online', subline: '', raw: { name: 'external', label: 'external' }, server,
};

function button(label: string): HTMLButtonElement {
	const match = [...document.querySelectorAll('button')].find(b => b.textContent?.trim() === label);
	if (!match) throw new Error(`no button "${label}"`);
	return match as HTMLButtonElement;
}

describe('ProviderSettingsModal remove', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		removeNetworkServer.mockClear();
		resetSettingsNavigation();
		vi.unstubAllGlobals();
	});

	function open(onClose = vi.fn()) {
		// A web view that never shows the native dialog answers false.
		vi.stubGlobal('confirm', vi.fn(() => false));
		component = mount(ProviderSettingsModal, {
			target: document.body, props: { provider: provider as never, onClose },
		});
		flushSync();
		return onClose;
	}

	it('titles an External endpoint by its own name', () => {
		open();
		expect(document.querySelector('.head .title')?.textContent).toContain('Token Plan');
		expect(document.querySelector('.head .title')?.textContent).not.toContain('OpenAI-compatible API');
	});

	it('asks in the app, then removes this endpoint and closes', async () => {
		const onClose = open();

		button('Remove provider…').click();
		flushSync();
		expect(document.querySelector('[role="alertdialog"]')?.textContent).toContain('Remove Token Plan?');
		button('Remove').click();
		await vi.waitFor(() => expect(onClose).toHaveBeenCalled());

		expect(removeNetworkServer).toHaveBeenCalledWith('ns-plan');
		expect(window.confirm).not.toHaveBeenCalled();
	});

	it('cancel keeps the provider', () => {
		const onClose = open();

		button('Remove provider…').click();
		flushSync();
		button('Cancel').click();
		flushSync();

		expect(removeNetworkServer).not.toHaveBeenCalled();
		expect(onClose).not.toHaveBeenCalled();
		expect(window.confirm).not.toHaveBeenCalled();
	});

	it('opens a harness provider on Accounts with its canonical name and email label', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ accounts: [{
			id: 'codex-default', provider: 'codex', name: 'Default', is_default: true,
			state: 'legacy', revision: 1, identity: {},
			label: { name: 'Personal', email: 'person@example.test', text: 'Personal · person@example.test' },
		}] })));
		component = mount(ProviderSettingsModal, {
			target: document.body,
			props: { provider: {
				name: 'codex', label: 'Codex', type: 'harness', statusDot: 'online', subline: '1 account · 3 models',
				raw: { name: 'codex', label: 'Codex', type: 'harness' },
			} as never, onClose: vi.fn() },
		});
		await vi.waitFor(() => expect(document.querySelector('.account-usage-row .name')?.textContent).toBe('Personal'));
		expect(document.querySelector('.account-usage-row .email')?.textContent).toBe('person@example.test');
		expect(document.body.textContent).toContain('Add account');
	});

	it.each([
		{ name: 'claude-code', label: 'Claude Code' },
		{ name: 'codex', label: 'Codex' },
	])('keeps %s harness settings to Accounts and Overview without provider removal', async ({ name, label }) => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ accounts: [] })));
		component = mount(ProviderSettingsModal, {
			target: document.body,
			props: { provider: {
				name, label, type: 'harness', statusDot: 'online', subline: '1 model',
				raw: { name, label, type: 'harness' },
			} as never, onClose: vi.fn() },
		});
		await vi.waitFor(() => expect([...document.querySelectorAll('.tabs .tab')].map((tab) => tab.textContent)).toEqual(['Accounts', 'Overview']));
		button('Overview').click();
		flushSync();
		expect(document.body.textContent).not.toContain('Remove provider…');
	});

	it('starts a stopped managed model server through its lifecycle action', async () => {
		const fetcher = vi.fn().mockResolvedValue(Response.json({ ok: true, message: 'Model server started.' }));
		vi.stubGlobal('fetch', fetcher);
		const local = { ...server, id: 'ns-local', provider: 'omlx', kind: 'managed' as const, status: 'offline' as const, endpoints: [{ host: '127.0.0.1' }] };
		component = mount(ProviderSettingsModal, {
			target: document.body,
			props: { provider: { ...provider, name: 'omlx', type: 'local', server: local } as never, onClose: vi.fn() },
		});
		flushSync();
		button('Start').click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('Model server started.'));
		expect(fetcher).toHaveBeenCalledOnce();
		expect(String(fetcher.mock.calls[0][0])).toContain('/ns-local/lifecycle/start');
		expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'POST' });
	});

	it('shows the reference beside a failed model list refresh', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({
			ok: false, message: 'Couldn’t refresh the model list. Try again.', ref: 'local-ab12',
		})));
		const local = { ...server, id: 'ns-local', provider: 'omlx', kind: 'network' as const, endpoints: [{ host: '127.0.0.1' }] };
		component = mount(ProviderSettingsModal, {
			target: document.body,
			props: { provider: { ...provider, name: 'omlx', type: 'local', server: local } as never, onClose: vi.fn() },
		});
		flushSync();
		button('Reload models').click();
		await vi.waitFor(() => expect(document.body.textContent).toContain(
			'Couldn’t refresh the model list. Try again. Reference: local-ab12',
		));
	});
});
