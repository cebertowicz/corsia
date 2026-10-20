import { writable, derived, get } from 'svelte/store';
import { token } from './auth';
import { providers as providersStore, type ProviderEntry } from './providers';
import { networkServers, loadNetworkServers, type NetworkServer } from './networkServers';
import { fetchCatalogInventory, harnessSummaries, type HarnessSummary } from './harnessProviders';
import { readWorkspaceModels } from './modelsLoad';
import { vendorAccounts } from './vendorAccounts';

const BASE = '';
const t = () => get(token) || '';

export interface HostResources {
	device_name?: string;
	total_gb: number;
	free_gb: number;
	models_used_gb: number;
	other_used_gb: number;
	safe_free_gb: number;
	safe_reserve_gb: number;
	by_provider: Record<string, { models: { model_id: string; memory_gb: number }[]; used_gb: number }>;
}

export interface HostProvider {
	name: string;
	label: string;
	type: 'cloud' | 'local' | 'network' | 'harness';
	statusDot: 'online' | 'idle' | 'error' | 'disabled';
	subline: string;
	raw: ProviderEntry;
	server?: NetworkServer;
	/** The fetched CLI inventory total; account counts are derived from vendorAccounts. */
	harnessModelCount?: number;
}

/** Whether a card's switch reads on. An External endpoint is switched on its own. */
export function providerEnabled(p: HostProvider): boolean {
	if (p.server?.kind === 'external') return p.server.status !== 'disabled';
	return p.type === 'cloud' ? cloudProviderOn(p.raw) : p.raw.enabled !== false;
}

export interface HostGroup {
	key: string;
	name: string;
	subname: string;
	netKind: 'localhost' | 'lan' | 'tailscale' | 'cloud';
	resources?: HostResources;
	providers: HostProvider[];
}

// Network/resources/inventory loading owns this snapshot. `hostGroups` below is the
// canonical display owner: it derives the account-sensitive harness card text, and reads
// each server from `networkServers`, so a saved server field shows without a reload.
export const hostGroupSnapshot = writable<HostGroup[]>([]);
export const hostGroups = derived(
	[hostGroupSnapshot, vendorAccounts, networkServers],
	([$snapshot, $accounts, $servers]) => $snapshot.map((group) => ({
		...group,
		providers: group.providers.map((provider) => {
			const server = provider.server && ($servers.find((row) => row.id === provider.server?.id) ?? provider.server);
			const live = server === provider.server ? provider : { ...provider, server };
			return live.type === 'harness' && live.harnessModelCount != null
				? { ...live, subline: _harnessSubline(live.name, live.harnessModelCount, $accounts) }
				: live;
		}),
	})),
);
export const hostGroupsLoading = writable(false);
/** Set when the Claude Code and Codex cards could not load, so an absent card is never read as "no models". */
export const harnessInventoryError = writable('');

async function fetchHostResources(): Promise<HostResources | null> {
	try {
		const res = await fetch(`${BASE}/api/system/host-resources?token=${t()}`);
		if (!res.ok) return null;
		return await res.json();
	} catch {
		return null;
	}
}

/** The one rule for a cloud provider being on: it has a key and its switch is not off. */
export const cloudProviderOn = (p: ProviderEntry) => !!p.has_key && p.enabled !== false;

function _modelCountLabel(count: number): string {
	return `${count} model${count === 1 ? '' : 's'}`;
}

function _harnessSubline(name: string, modelCount: number, accounts = get(vendorAccounts)): string {
	const accountCount = accounts.filter((account) => account.provider === name && account.state !== 'removed').length;
	return accountCount > 0 ? `${accountCount} account${accountCount === 1 ? '' : 's'} · ${_modelCountLabel(modelCount)}` : _modelCountLabel(modelCount);
}

function _statusDotForServer(s: NetworkServer): HostProvider['statusDot'] {
	if (s.status === 'online') return 'online';
	if (s.status === 'offline') return 'idle';
	if (s.status === 'disabled') return 'disabled';
	return 'idle';
}

function _labelForServerProvider(s: NetworkServer, raw: ProviderEntry | undefined): string {
	if (s.provider === 'omlx') return 'oMLX';
	if (s.kind === 'external') {
		return s.dialect === 'anthropic' ? 'Anthropic-compatible API' : 'OpenAI-compatible API';
	}
	return raw?.label || s.provider;
}

// A user-registered endpoint on a public host is a cloud service, not a machine
// on this network; the endpoint kind is the backend's own host classification.
function _isCloudEndpoint(s: NetworkServer): boolean {
	return s.kind === 'external' && (s.endpoints || []).some(e => e.kind === 'public');
}

function _serverProvider(
	s: NetworkServer,
	providers: ProviderEntry[],
	type: HostProvider['type'],
): HostProvider {
	const raw = providers.find(p => p.name === s.provider);
	return {
		name: s.provider,
		label: _labelForServerProvider(s, raw),
		type,
		statusDot: _statusDotForServer(s),
		subline: _networkSubline(s),
		raw: raw ?? ({ name: s.provider, label: s.provider } as ProviderEntry),
		server: s,
	};
}

function _formatTimeAgo(iso: string | null | undefined): string {
	if (!iso) return 'never';
	const ms = Date.now() - new Date(iso).getTime();
	const min = Math.floor(ms / 60000);
	if (min < 1) return 'just now';
	if (min < 60) return `${min}m ago`;
	const hr = Math.floor(min / 60);
	if (hr < 24) return `${hr}h ago`;
	const d = Math.floor(hr / 24);
	return `${d}d ago`;
}

export function groupByHost(
	providers: ProviderEntry[],
	servers: NetworkServer[],
	resources: HostResources | null,
	harnesses: HarnessSummary[] = [],
): HostGroup[] {
	const groups: HostGroup[] = [];

	const localServers: NetworkServer[] = [];
	const cloudEndpoints: NetworkServer[] = [];
	const remoteHosts: Map<string, NetworkServer[]> = new Map();
	for (const s of servers) {
		if (s.status === 'removed') continue;
		const isLocal = (s.endpoints || []).some(e => e.host === '127.0.0.1' || e.host === 'localhost');
		if (isLocal) {
			localServers.push(s);
		} else if (_isCloudEndpoint(s)) {
			cloudEndpoints.push(s);
		} else {
			const hostKey = s.machine_id || s.instance_id;
			const arr = remoteHosts.get(hostKey) || [];
			arr.push(s);
			remoteHosts.set(hostKey, arr);
		}
	}

	const localServerProviders: HostProvider[] = localServers.map(s => _serverProvider(s, providers, 'local'));

	if (localServerProviders.length) {
		const deviceName = resources?.device_name?.trim() || 'This Mac';
		groups.push({
			key: 'this-mac',
			name: deviceName,
			subname: _thisMacSubname(resources),
			netKind: 'localhost',
			resources: resources ?? undefined,
			providers: localServerProviders,
		});
	}

	for (const [hostKey, ss] of [...remoteHosts.entries()].sort()) {
		const first = ss[0];
		const isTailscale = (first.endpoints || []).some(e => e.host?.startsWith('100.'));
		groups.push({
			key: `host-${hostKey}`,
			name: first.instance_name || hostKey.slice(0, 8),
			subname: _remoteHostSubname(first),
			netKind: isTailscale ? 'tailscale' : 'lan',
			providers: ss.map(s => _serverProvider(s, providers, 'network')),
		});
	}

	for (const s of cloudEndpoints) {
		groups.push({
			key: `endpoint-${s.id}`,
			name: s.instance_name || s.endpoints?.[0]?.host || s.id,
			subname: _remoteHostSubname(s),
			netKind: 'cloud',
			providers: [_serverProvider(s, providers, 'network')],
		});
	}

	const cloudProviders = providers.filter(p => p.type === 'cloud' && p.has_key);
	if (cloudProviders.length) {
		groups.push({
			key: 'cloud',
			name: 'Cloud',
			subname: 'API providers',
			netKind: 'cloud',
			providers: cloudProviders.map(p => ({
				name: p.name,
				label: p.label,
				type: 'cloud',
				statusDot: cloudProviderOn(p) ? 'online' : 'disabled',
				subline: _cloudSubline(p),
				raw: p,
			})),
		});
	}

	if (harnesses.length) {
		groups.push({
			key: 'agent-clis',
			name: 'Agent CLIs',
			subname: 'Run by the CLI on this Mac',
			netKind: 'localhost',
			providers: harnesses.map(h => ({
				name: h.name,
				label: h.label,
				type: 'harness',
				statusDot: h.ready > 0 ? 'online' : 'idle',
				subline: _harnessSubline(h.name, h.total),
				harnessModelCount: h.total,
				raw: { name: h.name, label: h.label, type: 'harness' } as ProviderEntry,
			})),
		});
	}

	return groups;
}

function _thisMacSubname(r: HostResources | null): string {
	if (!r) return 'localhost';
	return `${r.total_gb.toFixed(0)} GB memory`;
}

function _remoteHostSubname(s: NetworkServer): string {
	const ip = s.endpoints?.[0]?.host || '';
	const seen = _formatTimeAgo(s.last_seen_at);
	return `${ip} · last seen ${seen}`;
}

function _networkSubline(s: NetworkServer): string {
	if (s.kind === 'external') {
		// Registered by URL: the URL is the address; a port says nothing here.
		const url = s.api_base || '';
		return s.status === 'online' ? url : ['off', url].filter(Boolean).join(' · ');
	}
	const port = s.port ? `port ${s.port}` : '';
	if (s.status !== 'online') return ['off', port].filter(Boolean).join(' · ');
	const used = s.memory_used_mb ? `${(s.memory_used_mb / 1024).toFixed(0)} GiB used` : '';
	return [port, used].filter(Boolean).join(' · ');
}

function _cloudSubline(p: ProviderEntry): string {
	const models = _modelCountLabel(p.models?.length || 0);
	return cloudProviderOn(p) ? models : `off · ${models}`;
}

export async function loadHostGroups(): Promise<void> {
	hostGroupsLoading.set(true);
	try {
		await loadNetworkServers();
		const [resources, harnessData] = await Promise.all([
			fetchHostResources(),
			// Settings needs both projections; either failure is visible instead of becoming an empty card.
			Promise.all([fetchCatalogInventory(), readWorkspaceModels()]).then(
				([inventory, catalog]) => { harnessInventoryError.set(''); return { inventory, catalog }; },
				(e) => {
					console.error('harness inventory failed:', e);
					harnessInventoryError.set('Could not load the Claude Code and Codex models. Reload the page to try again.');
					return null;
				},
			),
		]);
		const provs = get(providersStore);
		const servers = get(networkServers);
		hostGroupSnapshot.set(groupByHost(provs, servers, resources, harnessData ? harnessSummaries(harnessData.inventory, harnessData.catalog) : []));
	} catch (e) {
		console.error('loadHostGroups failed:', e);
		hostGroupSnapshot.set([]);
	} finally {
		hostGroupsLoading.set(false);
	}
}

export const liveHostGroups = derived(
	[providersStore, networkServers],
	([$providers, $servers]) => {
		return groupByHost($providers, $servers, null);
	},
);
