import { writable, get } from 'svelte/store';
import { token } from './auth';
import { plainErrorText, SERVER_UNREACHABLE } from '../plainError';

export interface ProviderEntry {
	name: string;
	label: string;
	/** 'harness' is a Claude Code or Codex card the Providers view builds from the catalog. */
	type: 'cloud' | 'network' | 'harness';
	has_key: boolean;
	masked_key: string | null;
	api_base: string | null;
	docs_url: string | null;
	models: string[];
	enabled?: boolean;
	auto_start?: boolean;
	auto_discover?: boolean;
	capabilities?: string[];
}

export const providers = writable<ProviderEntry[]>([]);

const BASE = '';
function t() { return get(token) || ''; }

export async function loadProviders(): Promise<void> {
	try {
		const res = await fetch(`${BASE}/api/settings/providers?token=${t()}`);
		if (res.ok) {
			const data = await res.json();
			providers.set(data.providers || []);
		}
	} catch (e) {
		console.error('Failed to load providers:', e);
	}
}

export async function updateProvider(
	name: string,
	patch: {
		api_key?: string;
		api_base?: string | null;
		enabled?: boolean;
		auto_discover?: boolean;
		auto_start?: boolean;
	},
): Promise<{ ok: boolean; error?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/providers/${name}?token=${t()}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(patch),
		});
		if (!res.ok) {
			const data = await res.json().catch(() => null);
			if (data?.detail !== undefined) console.error('[providers] update rejected', data.detail);
			return {
				ok: false,
				error: plainErrorText(data?.detail, 'Could not save this provider. Try again.'),
			};
		}
		await loadProviders();
		return { ok: true };
	} catch (e) {
		console.error('[providers] update request failed', e);
		return { ok: false, error: SERVER_UNREACHABLE };
	}
}

export async function deleteProvider(name: string): Promise<{ ok: boolean }> {
	try {
		const res = await fetch(`${BASE}/api/settings/providers/${name}?token=${t()}`, { method: 'DELETE' });
		if (res.ok) await loadProviders();
		return { ok: res.ok };
	} catch {
		return { ok: false };
	}
}

export async function testProvider(name: string): Promise<{ ok: boolean; message?: string; error?: string }> {
	try {
		const res = await fetch(`${BASE}/api/settings/providers/${name}/test?token=${t()}`, { method: 'POST' });
		return await res.json();
	} catch (e) {
		return { ok: false, error: String(e) };
	}
}
