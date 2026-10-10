import { writable, get } from 'svelte/store';
import { token } from './auth';
import type { ProviderEntry } from './providers';
import type { NetworkServer } from './networkServers';
import { plainErrorText, SERVER_UNREACHABLE } from '../plainError';
import {
	preserveTransitional,
} from './providerModelUtils';
import {
	cloudProviderModels,
	fallbackCloudModels,
	networkProviderModels,
} from './providerModelRows';
import { harnessModels } from './harnessProviders';

export type ModelState = 'loaded' | 'loading' | 'cache_warming' | 'queued' | 'error' | 'default';
export type ModelCapability = 'vision' | 'reasoning' | 'tools';

export interface ProviderModel {
	id: string;
	name: string;
	state: ModelState;
	progress?: number;
	meta: string;
	caps: ModelCapability[];
	contextLength?: number;
	/** External rows: the provider or per-server window, before any per-model checkpoint window. */
	providerContextLength?: number | null;
	sizeBytes?: number;
	lastError?: string;
	jobId?: string;
	dbId?: string;
	/** External rows: how image support was settled; null until first checked. */
	imageInput?: ImageInput | null;
	/** Harness rows: the user's label, and the name discovery reported. */
	alias?: string | null;
	discoveredName?: string;
}

export interface ImageInput {
	source: 'detected' | 'user';
	result: 'yes' | 'no' | 'unknown';
	detail: string;
	checked_at: string;
}

interface ProviderModelsState {
	loading: boolean;
	models: ProviderModel[];
	lastError?: string;
}

const _models = writable<Record<string, ProviderModelsState>>({});
export const providerModels = { subscribe: _models.subscribe };

const BASE = '';
const t = () => get(token) || '';

function _key(providerName: string, serverId?: string): string {
	return serverId ? `${providerName}|${serverId}` : providerName;
}

function _setState(key: string, patch: Partial<ProviderModelsState>) {
	_models.update(s => {
		const prev = s[key] ?? { loading: false, models: [] };
		return { ...s, [key]: { ...prev, ...patch } };
	});
}

export async function loadProviderModelsFor(p: ProviderEntry, server?: NetworkServer): Promise<void> {
	const k = _key(p.name, server?.id);
	_setState(k, { loading: true, lastError: undefined });

	try {
		if (p.type === 'harness') return _setState(k, { loading: false, models: await harnessModels(p.name) });
		if (p.type === 'cloud') {
			const res = await fetch(`${BASE}/api/settings/providers/${p.name}/models?token=${t()}`);
			if (!res.ok) {
				const list = fallbackCloudModels(p.models);
				_setState(k, { loading: false, models: list });
				return;
			}
			const data = await res.json();
			const list = cloudProviderModels(data);
			_setState(k, {
				loading: false,
				models: preserveTransitional(get(_models)[k]?.models, list),
			});
			return;
		}

		if (server) {
			const res = await fetch(`${BASE}/api/settings/network-servers/${server.id}/models?token=${t()}`);
			if (!res.ok) { _setState(k, { loading: false, lastError: `HTTP ${res.status}` }); return; }
			const data = await res.json();
			const deduped = networkProviderModels(data);
			const next = preserveTransitional(get(_models)[k]?.models, deduped);
			_setState(k, { loading: false, models: next });
		}
	} catch (e) {
		_setState(k, { loading: false, lastError: String(e) });
	}
}

export async function pollJobsFor(p: ProviderEntry, server?: NetworkServer): Promise<void> {
	if (p.type === 'cloud') return;

	const k = _key(p.name, server?.id);

	let jobs: any[] = [];
	try {
		if (server) {
			const res = await fetch(`${BASE}/api/settings/network-servers/${server.id}/jobs/active?token=${t()}`);
			if (res.ok) jobs = (await res.json()).jobs ?? [];
		}
	} catch { return; }

	let warmReconciled = false;

	_models.update(s => {
		const cur = s[k];
		if (!cur) return s;
		const byTarget: Record<string, any> = {};
		for (const j of jobs) {
			if (j.target) byTarget[j.target] = j;
		}
		let mutated = false;
		const next = cur.models.map(m => {
			const j = byTarget[m.id];
			if (!j) {
				if (m.state === 'cache_warming') {
					mutated = true;
					warmReconciled = true;
					return { ...m, state: 'loaded' as ModelState, progress: undefined, jobId: undefined };
				}
				return m;
			}
			const pct = j.progress_total ? Math.round((j.progress_current / j.progress_total) * 100) : 0;
			let state: ModelState = 'queued';
			if (j.state === 'running') {
				state = j.kind === 'warm' ? 'cache_warming' : 'loading';
			} else if (j.state === 'pending') {
				state = 'queued';
			}
			const eta = j.eta_seconds != null ? ` · ~${Math.round(j.eta_seconds)}s` : '';
			const meta = state === 'cache_warming'
				? `warming cache · ${j.progress_current ?? 0} / ${j.progress_total ?? '?'} tokens${eta}`
				: state === 'loading'
					? `loading · ${j.progress_current ?? 0} / ${j.progress_total ?? '?'}${eta}`
					: 'queued';
			mutated = true;
			return { ...m, state, progress: pct, meta, jobId: j.id };
		});
		return mutated ? { ...s, [k]: { ...cur, models: next } } : s;
	});

	if (warmReconciled) {
		loadProviderModelsFor(p, server).catch(() => {});
	}
}

export function markModelOptimistic(
	p: ProviderEntry,
	server: NetworkServer | undefined,
	modelId: string,
	nextState: ModelState,
): void {
	const k = _key(p.name, server?.id);
	_models.update(s => {
		const cur = s[k];
		if (!cur) return s;
		return {
			...s,
			[k]: {
				...cur,
				models: cur.models.map(m =>
					m.id === modelId ? { ...m, state: nextState } : m,
				),
			},
		};
	});
}

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
	if (!dbId) return Promise.resolve({ ok: false, message: 'This model is not in the catalog yet. Refresh and try again.' });
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
			return { ok: res.ok, message: data.message };
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

export function modelsForProvider(state: Record<string, ProviderModelsState>, p: ProviderEntry, server?: NetworkServer): ProviderModelsState {
	const k = _key(p.name, server?.id);
	return state[k] ?? { loading: false, models: [] };
}
