import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import ProvidersV7View from './ProvidersV7View.svelte';
import { emit } from '$lib/ws/events';

const calls = vi.hoisted(() => ({ servers: vi.fn(async () => {}), models: vi.fn() }));
vi.mock('$lib/stores/networkServers', async (original) => ({
	...await original<object>(), loadNetworkServers: calls.servers,
}));
vi.mock('$lib/stores/providersV7', async (original) => ({
	...await original<object>(), loadHostGroups: vi.fn(async () => {}),
}));
vi.mock('$lib/stores/providers', async (original) => ({
	...await original<object>(), loadProviders: vi.fn(async () => {}),
}));
vi.mock('$lib/stores/vendorAccounts', async (original) => ({
	...await original<object>(), loadVendorAccounts: vi.fn(async () => {}),
}));
vi.mock('$lib/stores/models', async (original) => ({
	...await original<object>(), loadModels: calls.models,
}));

let component: ReturnType<typeof mount> | undefined;
afterEach(() => {
	if (component) unmount(component);
	component = undefined;
	calls.servers.mockClear();
	calls.models.mockClear();
	document.body.innerHTML = '';
});

it('adds list reads only for server lifecycle transitions while Providers is open', async () => {
	component = mount(ProvidersV7View, { target: document.body });
	await vi.waitFor(() => expect(calls.models).toHaveBeenCalledOnce());
	expect(calls.servers).toHaveBeenCalledOnce();
	for (let i = 0; i < 10; i++) emit('models_updated', { type: 'models_updated' });
	expect(calls.servers).toHaveBeenCalledOnce();
	// The existing event identifies the server at restart admission and completion.
	emit('models_updated', { type: 'models_updated', server_id: 'seat' });
	emit('models_updated', { type: 'models_updated', server_id: 'seat' });
	expect(calls.servers).toHaveBeenCalledTimes(3);
	unmount(component);
	component = undefined;
	emit('models_updated', { type: 'models_updated', server_id: 'seat' });
	expect(calls.servers).toHaveBeenCalledTimes(3);
});
