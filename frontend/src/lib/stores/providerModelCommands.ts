import type { ProviderEntry } from './providers';
import type { NetworkServer } from './networkServers';
import { plainErrorText, SERVER_UNREACHABLE } from '../plainError';
import { token } from './auth';
import { get } from 'svelte/store';

/** What the model rows ask a server to do: load, unload and cancel a job, or switch a catalog row on or off. */

const BASE = '';
const t = () => get(token) || '';

// Cloud, External and harness models are catalog rows something else runs: offering
// one is the catalog's enabled flag, written by row id. Only managed servers load.
async function _setCatalogModelEnabled(modelDbId: string, enable: boolean): Promise<{ ok: boolean; message?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/models/${encodeURIComponent(modelDbId)}?token=${t()}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled: enable }),
		});
		const data = await res.json().catch(() => null);
		if (res.ok) return { ok: true };
		console.error('[models] enable/disable rejected', data?.error ?? data?.detail);
		return {
			ok: false,
			message: plainErrorText(
				data?.error ?? data?.detail,
				enable ? 'Could not enable this model. Try again.' : 'Could not disable this model. Try again.',
			),
		};
	} catch (e) {
		console.error('[models] enable/disable request failed', e);
		return { ok: false, message: SERVER_UNREACHABLE };
	}
}

function _catalogRowToggle(
	p: ProviderEntry,
	server: NetworkServer | undefined,
	modelId: string,
	dbId: string | undefined,
	enable: boolean,
): Promise<{ ok: boolean; message?: string }> | null {
	if (p.type === 'cloud' || p.type === 'harness') return _setCatalogModelEnabled(modelId, enable);
	if (server?.kind !== 'external') return null;
	if (!dbId) return Promise.resolve({ ok: false, message: "Couldn’t find this model. Refresh and try again." });
	return _setCatalogModelEnabled(dbId, enable);
}

export async function loadModel(
	p: ProviderEntry,
	server: NetworkServer | undefined,
	modelId: string,
	dbId?: string,
): Promise<{ ok: boolean; message?: string }> {
	const toggled = _catalogRowToggle(p, server, modelId, dbId, true);
	if (toggled) return toggled;
	if (server) {
		try {
			const res = await fetch(`${BASE}/api/settings/network-servers/${server.id}/models/load?token=${t()}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ model: modelId }),
			});
			const data = await res.json().catch(() => ({}));
			return { ok: res.ok, message: data.message ?? data.detail };
		} catch (e) {
			return { ok: false, message: String(e) };
		}
	}
	return { ok: false, message: 'no server' };
}

export async function cancelJob(
	server: NetworkServer | undefined,
	jobId: string,
): Promise<{ ok: boolean; message?: string }> {
	if (!server) return { ok: false, message: 'no server' };
	try {
		const res = await fetch(
			`${BASE}/api/settings/network-servers/${server.id}/jobs/${jobId}?token=${t()}`,
			{ method: 'DELETE' },
		);
		const data = await res.json().catch(() => null);
		if (res.ok) return { ok: true, message: data?.message };
		console.error('[models] cancel job rejected', data?.message ?? data?.detail);
		return {
			ok: false,
			message: plainErrorText(
				data?.message ?? data?.detail,
				'Could not cancel this download. Try again.',
			),
		};
	} catch (e) {
		console.error('[models] cancel job request failed', e);
		return { ok: false, message: SERVER_UNREACHABLE };
	}
}

export async function unloadModel(
	p: ProviderEntry,
	server: NetworkServer | undefined,
	modelId: string,
	dbId?: string,
): Promise<{ ok: boolean; message?: string }> {
	const toggled = _catalogRowToggle(p, server, modelId, dbId, false);
	if (toggled) return toggled;
	if (server) {
		try {
			const res = await fetch(`${BASE}/api/settings/network-servers/${server.id}/models/unload?token=${t()}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ model: modelId }),
			});
			const data = await res.json().catch(() => ({}));
			return { ok: res.ok, message: data.message };
		} catch (e) {
			return { ok: false, message: String(e) };
		}
	}
	return { ok: false, message: 'no server' };
}
