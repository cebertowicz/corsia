import { writable, get } from 'svelte/store';
import { token } from './auth';

export type EndpointKind = 'local' | 'lan' | 'tailscale' | 'public';

export interface NetworkEndpoint {
	id: string;
	server_id: string;
	host: string;
	kind: EndpointKind;
	last_ok_at: string | null;
	last_latency_ms: number | null;
}

export interface ExternalDiscoveryResult {
	at: string;
	models: number;
	error: string | null;
	// Why the published reasoning levels could not be read, if they could not.
	reasoning_levels_error?: string | null;
}

export interface NetworkServerMetadata {
	base_url?: string;
	discovery?: ExternalDiscoveryResult;
	// Ids the user declared because the endpoint does not list its models.
	declared_models?: string[];
}

/** Model ids typed one per line or comma-separated, trimmed and de-duplicated. */
export function parseModelIds(text: string): string[] {
	return [...new Set(text.split(/[\s,]+/).map(s => s.trim()).filter(Boolean))];
}

export interface ServerBilling {
	mode: 'subscription' | 'pay_per_use';
	monthly_price_cents: number | null;
	charge_day: number | null;
}

export interface NetworkServer {
	id: string;
	provider: string;
	instance_id: string;
	machine_id?: string | null;
	instance_name: string;
	kind?: 'managed' | 'external' | null;
	dialect?: 'openai' | 'anthropic' | null;
	port: number;
	status: 'discovered' | 'online' | 'offline' | 'disabled' | 'removed';
	source: 'auto' | 'manual';
	latency_ms: number | null;
	memory_total_mb: number | null;
	memory_used_mb: number | null;
	gpu_info: string | null;
	endpoints: NetworkEndpoint[];
	active_host: string | null;
	api_base: string | null;
	mgmt_base: string | null;
	loaded_models?: { identifier: string; server_id: string; status: string }[];
	last_seen_at?: string | null;
	discovered_at?: string | null;
	metadata?: NetworkServerMetadata;
	/** The backend's fact that the endpoint is on this computer; it supplies the address caption only. */
	on_this_mac?: boolean;
	/** The person marked this machine as theirs, so its models count as local. */
	private?: boolean;
	has_key?: boolean;
	masked_key?: string;
	/** True only for CORSIA's own oMLX, online or not: identity, never status (read ``status`` for that). */
	corsia_omlx?: boolean;
	/** How the endpoint is billed; null when unset. */
	billing?: ServerBilling | null;
	/** Fences billing writes; sent back unchanged. */
	billing_revision?: number;
	/** The endpoint's models have a list price, so pay-per-use usage can be counted. */
	list_priced?: boolean;
}

export const networkServers = writable<NetworkServer[]>([]);

const BASE = '';
function t() { return get(token) || ''; }

export async function loadNetworkServers(): Promise<void> {
	try {
		const res = await fetch(`${BASE}/api/settings/network-servers?token=${t()}`);
		if (res.ok) {
			const data = await res.json();
			networkServers.set(data.servers || []);
		}
	} catch (e) {
		console.error('Failed to load network servers:', e);
	}
}

export async function addNetworkServer(
	name: string,
	hostname: string,
	port: number,
	// A cloud endpoint is identified by a full URL rather than host+port, and
	// carries its own auth. Optional so the existing oMLX-by-IP call stays exactly
	// as it was.
	extra?: { base_url?: string; dialect?: string; api_key?: string; model_ids?: string[] },
): Promise<{ ok: boolean; server_id?: string; status?: string; message?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/network-servers?token=${t()}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ name, hostname, port, ...(extra ?? {}) }),
		});
		const data = await res.json();
		if (data.ok) await loadNetworkServers();
		// A rejected request carries FastAPI's reason under `detail`, not `message`.
		// Without this the named failure (DNS miss, TLS refusal, timeout, HTTP status,
		// bad path) never reaches the form and every failure reads the same.
		if (!data.ok && typeof data.detail === 'string' && !data.message) {
			data.message = data.detail;
		}
		return data;
	} catch (e) {
		return { ok: false, message: String(e) };
	}
}

export async function updateNetworkServer(
	serverId: string,
	extra: {
		base_url: string; dialect: 'openai' | 'anthropic'; api_key?: string; model_ids?: string[];
	},
): Promise<{ ok: boolean; server_id?: string; status?: string; models?: number; message?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/network-servers/${serverId}?token=${t()}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(extra),
		});
		const data = await res.json();
		if (data.ok) await loadNetworkServers();
		if (!data.ok && typeof data.detail === 'string' && !data.message) data.message = data.detail;
		return data;
	} catch (e) {
		return { ok: false, message: String(e) };
	}
}

/** Record how an External endpoint is billed; a null mode clears it. */
export async function setNetworkServerBilling(
	server: NetworkServer,
	input: { mode: ServerBilling['mode'] | null; monthly_price_cents: number | null; charge_day: number | null },
): Promise<{ ok: boolean; message?: string; status?: number; code?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/network-servers/${server.id}/billing?token=${t()}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ ...input, expected_billing_revision: server.billing_revision ?? 0 }),
		});
		const data = await res.json().catch(() => ({}));
		if (!res.ok) return { ok: false, status: res.status, ...(typeof data.code === 'string' ? { code: data.code } : {}), message: typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}` };
		networkServers.update(servers => servers.map(row => row.id === server.id
			? { ...row, billing: data.billing ?? null, billing_revision: data.billing_revision, list_priced: data.list_priced } : row));
		return { ok: true };
	} catch (e) {
		return { ok: false, message: String(e) };
	}
}

/** Turn one server off (its models leave the picker) or back on. */
export async function setNetworkServerEnabled(
	serverId: string,
	enabled: boolean,
): Promise<{ ok: boolean; message?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/network-servers/${serverId}/enabled?token=${t()}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled }),
		});
		if (res.ok) {
			await loadNetworkServers();
			return { ok: true };
		}
		const data = await res.json().catch(() => ({}));
		return { ok: false, message: typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}` };
	} catch (e) {
		return { ok: false, message: String(e) };
	}
}

/** Mark a network server's machine as the person's own (private), or clear the mark. */
export async function setNetworkServerPrivate(
	serverId: string,
	isPrivate: boolean,
): Promise<{ ok: true } | { ok: false; detail?: unknown; reached: boolean }> {
	try {
		const res = await fetch(`${BASE}/api/settings/network-servers/${encodeURIComponent(serverId)}/private?token=${encodeURIComponent(t())}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ private: isPrivate }),
		});
		if (res.ok) {
			networkServers.update(servers => servers.map(row => row.id === serverId ? { ...row, private: isPrivate } : row));
			return { ok: true };
		}
		const data = await res.json().catch(() => null);
		return { ok: false, detail: data?.detail, reached: true };
	} catch {
		return { ok: false, reached: false };
	}
}

/** Probe an External endpoint now and rediscover its models. */
export async function testNetworkServer(serverId: string): Promise<{ ok: boolean; message: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/network-servers/${serverId}/test?token=${t()}`, {
			method: 'POST',
		});
		const data = await res.json().catch(() => ({}));
		await loadNetworkServers();
		if (res.ok && typeof data.message === 'string') return { ok: !!data.ok, message: data.message };
		return { ok: false, message: typeof data.detail === 'string' ? data.detail : `Test failed (HTTP ${res.status})` };
	} catch (e) {
		return { ok: false, message: String(e) };
	}
}

/** Pin an External model's image support, or (`auto`) detect it again now. */
export async function setModelImageInput(
	serverId: string,
	modelId: string,
	mode: 'auto' | 'yes' | 'no',
): Promise<{ ok: boolean; message?: string }> {
	try {
		const path = `${serverId}/models/${encodeURIComponent(modelId)}/image-input`;
		const res = await fetch(`${BASE}/api/settings/network-servers/${path}?token=${t()}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ mode }),
		});
		if (res.ok) return { ok: true };
		const data = await res.json().catch(() => ({}));
		return { ok: false, message: typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}` };
	} catch (e) {
		return { ok: false, message: String(e) };
	}
}

export async function removeNetworkServer(serverId: string): Promise<{ ok: boolean; message?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/network-servers/${serverId}?token=${t()}`, {
			method: 'DELETE',
		});
		if (res.ok) {
			networkServers.update(servers => servers.filter(s => s.id !== serverId));
			return { ok: true };
		}
		const data = await res.json().catch(() => ({}));
		return { ok: false, message: typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}` };
	} catch (e) {
		return { ok: false, message: String(e) };
	}
}
