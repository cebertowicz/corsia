import { afterEach, describe, it, expect } from 'vitest';
import { cloudProviderOn, groupByHost, providerEnabled } from '../providersV7';
import type { ProviderEntry } from '../providers';
import type { NetworkServer } from '../networkServers';
import { vendorAccounts, type VendorAccount } from '../vendorAccounts';

const account = (provider: string, id: string): VendorAccount => ({
	id, provider, name: 'Default', is_default: true, state: 'signed_in', revision: 0,
	identity: {}, label: { name: 'Default', email: null, text: 'Default', custom: null },
});

afterEach(() => vendorAccounts.set([]));

function p(overrides: Partial<ProviderEntry>): ProviderEntry {
	return {
		name: 'x', label: 'X', type: 'cloud',
		has_key: false, masked_key: null, api_base: null, docs_url: null,
		models: [],
		...overrides,
	};
}

function s(overrides: Partial<NetworkServer>): NetworkServer {
	return {
		id: 'srv-' + (overrides.provider ?? 'x'),
		provider: 'omlx',
		instance_id: 'inst-1',
		instance_name: 'Mac',
		port: 5100,
		status: 'online',
		source: 'auto',
		latency_ms: 5,
		memory_total_mb: 32 * 1024,
		memory_used_mb: null,
		gpu_info: null,
		endpoints: [],
		active_host: null,
		api_base: 'http://127.0.0.1:5100',
		mgmt_base: 'http://127.0.0.1:5100',
		...overrides,
	};
}

describe('groupByHost', () => {
	it('returns empty when no providers and no servers', () => {
		expect(groupByHost([], [], null)).toEqual([]);
	});

	it('ignores retired raw local provider payloads', () => {
		const legacyLocal = {
			...p({ name: 'lmstudio', label: 'LM Studio' }),
			type: 'local',
			server_status: { running: true, port: 1234 },
		} as unknown as ProviderEntry;

		expect(groupByHost([legacyLocal], [], null)).toEqual([]);
	});

	it('puts a localhost network server (omlx at 127.0.0.1) into This Mac as type=local', () => {
		const groups = groupByHost(
			[p({ name: 'omlx', type: 'network' })],
			[s({ provider: 'omlx', endpoints: [{ id: 'e', server_id: 'srv-1', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }] })],
			null,
		);
		expect(groups).toHaveLength(1);
		expect(groups[0].key).toBe('this-mac');
		expect(groups[0].providers[0].name).toBe('omlx');
		expect(groups[0].providers[0].label).toBe('Mac');
		expect(groups[0].providers[0].type).toBe('local');
		expect(groups[0].providers[0].raw.type).toBe('network');
	});

	it('shows distinct registered names for legacy and CORSIA-managed local oMLX rows', () => {
		const endpoint = (server_id: string) => ({ id: `e-${server_id}`, server_id, host: '127.0.0.1', kind: 'local' as const, last_ok_at: null, last_latency_ms: null });
		const groups = groupByHost(
			[p({ name: 'omlx', type: 'network' })],
			[
				s({ id: 'legacy', instance_id: '127.0.0.1:8000', instance_name: 'This Mac', endpoints: [endpoint('legacy')] }),
				s({ id: 'managed', instance_id: 'corsia-managed', instance_name: "CORSIA's own oMLX", endpoints: [endpoint('managed')] }),
			],
			null,
		);

		expect(groups).toHaveLength(1);
		expect(groups[0].name).toBe('This computer');
		expect(groups[0].providers.map(provider => provider.label)).toEqual(['This Mac', "CORSIA's own oMLX"]);
	});

	it('groups remote daemons sharing machine_id under one host', () => {
		const remoteOmlxPrimary = s({
			provider: 'omlx', instance_id: 'inst-1-1', machine_id: 'machine-A',
			instance_name: 'mac-mini',
			endpoints: [{ id: 'e1', server_id: 'srv-1', host: '192.168.1.42', kind: 'lan', last_ok_at: null, last_latency_ms: null }],
		});
		const remoteOmlxSecondary = s({
			provider: 'omlx', instance_id: 'inst-1-1', machine_id: 'machine-A',
			instance_name: 'mac-mini',
			endpoints: [{ id: 'e2', server_id: 'srv-1', host: '192.168.1.42', kind: 'lan', last_ok_at: null, last_latency_ms: null }],
		});
		const groups = groupByHost(
			[p({ name: 'omlx', type: 'network' }), p({ name: 'omlx', type: 'network' })],
			[remoteOmlxPrimary, remoteOmlxSecondary],
			null,
		);
		expect(groups).toHaveLength(1);
		expect(groups[0].key).toBe('host-machine-A');
		expect(groups[0].providers.map(x => x.name).sort()).toEqual(['omlx', 'omlx']);
	});

	it('falls back to instance_id when machine_id is absent (older daemons)', () => {
		const legacy = s({
			provider: 'omlx', instance_id: 'inst-X', instance_name: 'old-mac',
			endpoints: [{ id: 'e', server_id: 'srv', host: '10.0.0.5', kind: 'lan', last_ok_at: null, last_latency_ms: null }],
		});
		const groups = groupByHost([p({ name: 'omlx', type: 'network' })], [legacy], null);
		expect(groups[0].key).toBe('host-inst-X');
	});

	it('keeps type=network for remote-host omlx/oMLX', () => {
		const remote = s({
			provider: 'omlx', instance_id: 'r-99', instance_name: 'mac-mini',
			endpoints: [{ id: 'e1', server_id: 'srv-r', host: '192.168.1.50', kind: 'lan', last_ok_at: null, last_latency_ms: null }],
		});
		const groups = groupByHost([p({ name: 'omlx', type: 'network' })], [remote], null);
		expect(groups[0].key).toBe('host-r-99');
		expect(groups[0].providers[0].type).toBe('network');
		expect(groups[0].providers[0].label).toBe('oMLX');
	});

	it('uses an oMLX server’s registered name', () => {
		const groups = groupByHost(
			[p({ name: 'omlx', type: 'network' })],
			[s({ provider: 'omlx', endpoints: [{ id: 'e', server_id: 'srv-1', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }] })],
			null,
		);
		expect(groups[0].providers[0].label).toBe('Mac');
	});

	it('leaves unknown provider labels unchanged', () => {
		const groups = groupByHost(
			[
				p({ name: 'custom-network', type: 'network' }),
				p({ name: 'omlx', type: 'network' }),
			],
			[
				s({ provider: 'custom-network', port: 5100, endpoints: [{ id: 'e1', server_id: 'srv-custom', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }] }),
				s({ provider: 'omlx', port: 5200, endpoints: [{ id: 'e2', server_id: 'srv-omlx', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }] }),
			],
			null,
		);
		expect(groups).toHaveLength(1);
		const labels = groups[0].providers.map(x => x.label).sort();
		expect(labels).toEqual(['Mac', 'X']);
	});

	it('groups remote servers by instance_id, separate from This Mac', () => {
		const lan = s({
			provider: 'omlx', instance_id: 'remote-1', instance_name: 'mac-mini-2',
			endpoints: [{ id: 'e1', server_id: 'srv-1', host: '192.168.1.42', kind: 'lan', last_ok_at: null, last_latency_ms: null }],
		});
		const groups = groupByHost([p({ name: 'omlx', type: 'network' })], [lan], null);
		expect(groups).toHaveLength(1);
		expect(groups[0].key).toBe('host-remote-1');
		expect(groups[0].name).toBe('mac-mini-2');
		expect(groups[0].netKind).toBe('lan');
	});

	it('marks tailscale-prefixed hosts as tailscale', () => {
		const ts = s({
			provider: 'omlx', instance_id: 'r-2', instance_name: 'mbp-m4',
			endpoints: [{ id: 'e1', server_id: 'srv-2', host: '100.64.1.5', kind: 'tailscale', last_ok_at: null, last_latency_ms: null }],
		});
		const groups = groupByHost([], [ts], null);
		expect(groups[0].netKind).toBe('tailscale');
	});

	it('skips removed servers', () => {
		const removed = s({ provider: 'omlx', status: 'removed', endpoints: [{ id: 'e', server_id: 'srv-x', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }] });
		const groups = groupByHost([], [removed], null);
		expect(groups).toEqual([]);
	});

	it('only renders cloud group when at least one provider has a key', () => {
		const noKey = groupByHost([p({ name: 'anthropic', type: 'cloud', has_key: false })], [], null);
		expect(noKey.find(g => g.key === 'cloud')).toBeUndefined();

		const withKey = groupByHost([p({ name: 'anthropic', type: 'cloud', has_key: true, label: 'Anthropic' })], [], null);
		expect(withKey.find(g => g.key === 'cloud')).toBeDefined();
		expect(withKey.find(g => g.key === 'cloud')!.providers[0].label).toBe('Anthropic');
	});

	it('passes resources through into This Mac group', () => {
		const groups = groupByHost(
			[p({ name: 'omlx', type: 'network' })],
			[s({
				provider: 'omlx',
				endpoints: [{ id: 'e', server_id: 'srv-1', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }],
			})],
			{
				total_gb: 256, free_gb: 128, models_used_gb: 64, other_used_gb: 64,
				safe_free_gb: 96, safe_reserve_gb: 32, by_provider: {},
			},
		);
		expect(groups[0].resources?.total_gb).toBe(256);
		expect(groups[0].subname).toBe('256 GB memory');

		const lowMemory = groupByHost(
			[p({ name: 'omlx', type: 'network' })],
			[s({
				provider: 'omlx',
				endpoints: [{ id: 'e-low', server_id: 'srv-low', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }],
			})],
			{ total_gb: 16, free_gb: 4, models_used_gb: 0, other_used_gb: 12, safe_free_gb: 8, safe_reserve_gb: 4, by_provider: {} },
		);
		expect(lowMemory[0].subname).toBe('16 GB memory');
	});

	it('subline for an offline network server includes port', () => {
		const offline = s({ provider: 'omlx', status: 'offline', port: 5100, endpoints: [{ id: 'e', server_id: 'srv-x', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: null }] });
		const groups = groupByHost([p({ name: 'omlx', type: 'network' })], [offline], null);
		expect(groups[0].providers[0].subline).toContain('port 5100');
	});

	it('shows an endpoint registered on a public host as its own cloud group, named by URL not port', () => {
		const base = 'https://ws-x.us-east-1.maas.aliyuncs.com/compatible-mode/v1';
		const alibaba = s({
			id: 'ns-ali', provider: 'external', kind: 'external', dialect: 'openai',
			instance_name: 'Alibaba', port: 443, api_base: base, mgmt_base: null,
			endpoints: [{ id: 'e', server_id: 'ns-ali', host: 'ws-x.us-east-1.maas.aliyuncs.com', kind: 'public', last_ok_at: null, last_latency_ms: null }],
		});
		const groups = groupByHost([], [alibaba], null);
		expect(groups).toHaveLength(1);
		expect(groups[0].netKind).toBe('cloud');
		expect(groups[0].name).toBe('Alibaba');
		expect(groups[0].providers[0].label).toBe('OpenAI-compatible API');
		expect(groups[0].providers[0].subline).toBe(base);
		expect(groups[0].providers[0].subline).not.toContain('port');
	});

	it('keeps an endpoint on a LAN address in its LAN host group', () => {
		const box = s({
			provider: 'external', kind: 'external', dialect: 'anthropic', machine_id: 'box',
			api_base: 'http://10.0.0.5:9000/v1',
			endpoints: [{ id: 'e', server_id: 'srv-external', host: '10.0.0.5', kind: 'lan', last_ok_at: null, last_latency_ms: null }],
		});
		const groups = groupByHost([], [box], null);
		expect(groups[0].netKind).toBe('lan');
		expect(groups[0].providers[0].label).toBe('Anthropic-compatible API');
	});

	it('adds an Agent CLIs section with one card per harness that has models', () => {
		const groups = groupByHost([], [], null, [
			{ name: 'claude-code', label: 'Claude Code', total: 3, ready: 2 },
			{ name: 'codex', label: 'Codex', total: 2, ready: 0 },
		]);
		expect(groups).toHaveLength(1);
		expect(groups[0].name).toBe('Agent CLIs');
		const [claude, codex] = groups[0].providers;
		expect(claude).toMatchObject({ name: 'claude-code', type: 'harness', statusDot: 'online', subline: '3 models' });
		expect(claude.raw.type).toBe('harness');
		expect(codex.statusDot).toBe('idle');
	});

	it('uses singular model counts on harness and cloud cards', () => {
		const counts = [0, 1, 2];
		const expected = ['0 models', '1 model', '2 models'];
		const harnesses = groupByHost([], [], null, counts.map(total => ({
			name: `harness-${total}`, label: `Harness ${total}`, total, ready: total,
		})));
		const cloud = groupByHost(counts.map(count => p({
			name: `cloud-${count}`, label: `Cloud ${count}`, type: 'cloud', has_key: true,
			models: Array.from({ length: count }, (_, index) => `model-${index}`),
		})), [], null);

		expect(harnesses[0].providers.map(provider => provider.subline)).toEqual(expected);
		expect(cloud[0].providers.map(provider => provider.subline)).toEqual(expected);
	});

	it('reads a keyed cloud provider that is switched off as off, like an off endpoint', () => {
		const groups = groupByHost([
			p({ name: 'on', label: 'On', has_key: true, enabled: true, models: ['m'] }),
			p({ name: 'unset', label: 'Unset', has_key: true, models: ['m'] }),
			p({ name: 'off', label: 'Off', has_key: true, enabled: false, models: ['m', 'n'] }),
		], [], null);
		const cards = groups[0].providers;

		expect(cards.map(card => [card.name, card.statusDot, card.subline])).toEqual([
			['on', 'online', '1 model'],
			['unset', 'online', '1 model'],
			['off', 'disabled', 'off · 2 models'],
		]);
		expect(cards.map(card => providerEnabled(card))).toEqual([true, true, false]);
	});

	it('answers whether a cloud provider is on from one rule for the dot, the switch and the helper', () => {
		const entries = [
			p({ name: 'a', has_key: true, enabled: true }),
			p({ name: 'b', has_key: true }),
			p({ name: 'c', has_key: true, enabled: false }),
			p({ name: 'd', has_key: false, enabled: true }),
		];
		const cards = groupByHost(entries, [], null)[0].providers;

		const listed = entries.filter(entry => cards.some(card => card.name === entry.name));

		expect(entries.map(cloudProviderOn)).toEqual([true, true, false, false]);
		expect(listed.map(entry => entry.name)).toEqual(['a', 'b', 'c']);
		expect(cards.map(card => card.statusDot === 'online')).toEqual(listed.map(cloudProviderOn));
		expect(cards.map(providerEnabled)).toEqual(listed.map(cloudProviderOn));
	});

	it('shows loaded provider account counts with singular and plural copy', () => {
		vendorAccounts.set([
			account('codex', 'default'), account('codex', 'work'), account('claude-code', 'default'),
		]);
		const groups = groupByHost([], [], null, [
			{ name: 'codex', label: 'Codex', total: 3, ready: 3 },
			{ name: 'claude-code', label: 'Claude Code', total: 1, ready: 1 },
		]);

		expect(groups[0].providers.map(provider => provider.subline)).toEqual([
			'2 accounts · 3 models', '1 account · 1 model',
		]);
	});
});
