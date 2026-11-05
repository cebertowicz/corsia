import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { providerModels, modelsForProvider } from '../providerModels';
import { groupByHost, harnessInventoryError, hostGroupSnapshot, hostGroups, loadHostGroups } from '../providersV7';
import { vendorAccounts } from '../vendorAccounts';
import { networkServers, type NetworkServer } from '../networkServers';

const codexRow = { id: 'codex/gpt-5.6-sol', harness: 'codex', provider: 'codex', display_name: 'GPT-5.6-Sol', enabled: 1, available: 1 };

function stubFetch(inventoryOk: boolean, catalogStatus: string) {
	vi.stubGlobal('fetch', vi.fn(async (url: string) => {
		if (url.startsWith('/api/settings/models')) {
			return inventoryOk
				? new Response(JSON.stringify({ models: [codexRow] }), { status: 200 })
				: new Response('{}', { status: 500 });
		}
		if (url.startsWith('/api/models')) {
			return new Response(JSON.stringify({ models: [{
				id: codexRow.id, name: 'Codex · GPT-5.6-Sol', display_name: codexRow.display_name,
				connection: 'codex', provider: 'codex', backend: 'codex', size: '', speed: '',
				status: catalogStatus, available: true,
			}] }), { status: 200 });
		}
		return new Response('{}', { status: 404 });
	}));
}

describe('loadHostGroups harness cards', () => {
	afterEach(() => { vi.unstubAllGlobals(); vendorAccounts.set([]); });

	it('says the CLI cards failed instead of showing an empty page, and clears it on success', async () => {
		stubFetch(false, 'ready');
		await loadHostGroups();
		expect(get(harnessInventoryError)).toMatch(/Could not load the Claude Code and Codex models/);
		expect(get(hostGroups).some((g) => g.key === 'agent-clis')).toBe(false);

		stubFetch(true, 'ready');
		await loadHostGroups();
		expect(get(harnessInventoryError)).toBe('');
		const clis = get(hostGroups).find((g) => g.key === 'agent-clis');
		expect(clis?.providers[0].statusDot).toBe('online');
	});

	it('shows the existing harness inventory error when the catalog read fails, never an empty card', async () => {
		vi.stubGlobal('fetch', vi.fn(async (url: string) => {
			if (url.startsWith('/api/settings/models')) return new Response(JSON.stringify({ models: [codexRow] }), { status: 200 });
			if (url.startsWith('/api/models')) return new Response('{}', { status: 500 });
			return new Response('{}', { status: 404 });
		}));

		await loadHostGroups();

		expect(get(harnessInventoryError)).toMatch(/Could not load the Claude Code and Codex models/);
		expect(get(hostGroups).some((group) => group.key === 'agent-clis')).toBe(false);
	});

	it('summarizes harness account and model counts from their separate owners', async () => {
		vendorAccounts.set([
			{ id: 'one', provider: 'codex', name: 'One', is_default: true, state: 'signed_in', revision: 1, identity: {}, label: { name: 'One', email: null, text: 'One' } },
			{ id: 'two', provider: 'codex', name: 'Two', is_default: false, state: 'signed_in', revision: 1, identity: {}, label: { name: 'Two', email: null, text: 'Two' } },
		]);
		stubFetch(true, 'ready');
		await loadHostGroups();
		const clis = get(hostGroups).find((g) => g.key === 'agent-clis');
		expect(clis?.providers[0].subline).toBe('2 accounts · 1 model');
	});

	it('updates the harness card count from account mutations without refetching host groups', async () => {
		stubFetch(true, 'ready');
		await loadHostGroups();
		const codexSubline = () => get(hostGroups).find((g) => g.key === 'agent-clis')?.providers.find((p) => p.name === 'codex')?.subline;
		expect(codexSubline()).toBe('1 model');

		vendorAccounts.set([{ id: 'signed-in', provider: 'codex', name: 'One', is_default: true, state: 'signed_in', revision: 1, identity: {}, label: { name: 'One', email: null, text: 'One' } }]);
		expect(codexSubline()).toBe('1 account · 1 model');
		vendorAccounts.update((accounts) => [...accounts, { id: 'added', provider: 'codex', name: 'Two', is_default: false, state: 'signed_in', revision: 1, identity: {}, label: { name: 'Two', email: null, text: 'Two' } }]);
		expect(codexSubline()).toBe('2 accounts · 1 model');
		vendorAccounts.update((accounts) => accounts.map((account) => account.id === 'added' ? { ...account, state: 'removed' as const } : account));
		expect(codexSubline()).toBe('1 account · 1 model');
	});

	it('holds the harness models from the same two reads, so opening a card paints them before its own read returns', async () => {
		stubFetch(true, 'ready');
		await loadHostGroups();

		const held = modelsForProvider(get(providerModels), { name: 'codex' } as never).models;
		expect(held.map((m) => [m.id, m.state])).toEqual([[codexRow.id, 'loaded']]);
	});

	it('does not show an empty card when the picker cannot offer any of its models', async () => {
		stubFetch(true, 'unavailable');
		await loadHostGroups();
		expect(get(hostGroups).some((group) => group.key === 'agent-clis')).toBe(false);
	});
});

describe('hostGroups server rows', () => {
	afterEach(() => { hostGroupSnapshot.set([]); networkServers.set([]); });

	it('reads each server from the server list, so a saved Trusted device mark shows before the next snapshot', () => {
		const studio = {
			id: 's1', provider: 'omlx', instance_id: 'i1', instance_name: 'Studio', kind: 'managed', port: 8000, status: 'online',
			endpoints: [{ id: 'e1', server_id: 's1', host: '192.168.1.20', kind: 'lan' }], private: false,
		} as unknown as NetworkServer;
		hostGroupSnapshot.set(groupByHost([], [studio], null));
		networkServers.set([{ ...studio, private: true }]);

		const provider = get(hostGroups).flatMap((group) => group.providers).find((row) => row.server?.id === 's1');
		expect(provider?.server?.private).toBe(true);
	});
});
