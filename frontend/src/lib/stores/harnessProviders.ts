import { get } from 'svelte/store';
import { token } from './auth';
import { plainErrorText, SERVER_UNREACHABLE } from '../plainError';
import { checkpointsBelowProvider, humanCtx } from './providerModelUtils';
import { type Model } from './models';
import { readWorkspaceModels } from './modelsLoad';
import { HARNESSES, settingsHarnessModelIds } from './harnessMembership';
import type { ModelState, ProviderModel } from './providerModels';

// Claude Code and Codex run on the user's own CLI sign-in: nothing to configure,
// so their cards choose which discovered models the picker offers and, per
// account, whether to spend past the plan limit (vendorAccounts.ts).
// `vendor` is the brand word an account sentence uses ("All Claude accounts"), not the company's name;
// `label` is the product's, as the picker group header and the cards show it; membership owns both facts.

export interface HarnessSummary {
	name: string;
	label: string;
	enabled: boolean;
	total: number;
	/** Rows the chat picker can offer now: public catalog status `ready`, the picker's own rule. */
	ready: number;
}

type CatalogRow = Record<string, any>;

const t = () => get(token) || '';

/** The settings inventory, including unavailable rows kept for existing thread resolution. */
export async function fetchCatalogInventory(): Promise<CatalogRow[]> {
	const res = await fetch(`/api/settings/models?token=${t()}`);
	if (!res.ok) throw new Error(`HTTP ${res.status}`);
	return (await res.json()).models ?? [];
}

export function harnessSummaries(settingsRows: CatalogRow[], catalog: Model[]): HarnessSummary[] {
	const catalogById = new Map(catalog.map((model) => [model.id, model]));
	return HARNESSES.map(({ name, label }) => {
		const ids = settingsHarnessModelIds(settingsRows, name, catalog);
		const enabled = settingsRows.some((row) => row.harness === name && row.provider_enabled !== false);
		return { name, label, enabled, total: ids.length, ready: ids.filter((id) => catalogById.get(id)?.status === 'ready').length };
	}).filter((summary) => summary.total > 0);
}

export function harnessProviderModels(
	rows: CatalogRow[], harness: string, pickerCatalog: Model[],
): ProviderModel[] {
	const settingsRows = new Map(rows.map((row) => [row.id, row]));
	return settingsHarnessModelIds(rows, harness, pickerCatalog)
		.flatMap((id) => {
			const row = settingsRows.get(id);
			if (!row) return [];
			const providerWindow = row.provider_context_length ?? row.context_window;
			const checkpointWindow = row.context_length ?? providerWindow;
			const meta = [
				providerWindow ? `${humanCtx(providerWindow)} context` : '',
				checkpointsBelowProvider(row) && checkpointWindow
					? `checkpoints at ${humanCtx(checkpointWindow)}`
					: '',
			].filter(Boolean).join(' · ');
			return [{
				id: row.id,
				name: row.alias || row.display_name || row.id,
				// On means offered in the picker; a hidden discovered model remains a Settings re-enable control.
				state: (row.enabled ? 'loaded' : 'default') as ModelState,
				meta,
				caps: [],
				contextLength: checkpointWindow ?? undefined,
				providerContextLength: providerWindow ?? undefined,
				dbId: row.id,
				alias: row.alias ?? null,
				discoveredName: row.display_name ?? row.id,
			}];
		});
}

/** Read catalog state directly so Settings can report a failed catalog read. */
export async function harnessModels(harness: string): Promise<ProviderModel[]> {
	const [settingsRows, catalog] = await Promise.all([fetchCatalogInventory(), readWorkspaceModels()]);
	return harnessProviderModels(settingsRows, harness, catalog);
}

/** Turn an agent CLI provider on or off, as every other provider's switch does. */
export async function setHarnessEnabled(name: string, enabled: boolean): Promise<{ ok: boolean; message?: string }> {
	try {
		const query = new URLSearchParams({ token: t() });
		const res = await fetch(`/api/providers/harnesses/${encodeURIComponent(name)}/enabled?${query}`, {
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ enabled }),
		});
		if (res.ok) return { ok: true };
		const data = await res.json().catch(() => null);
		return { ok: false, message: plainErrorText(data?.detail, 'Could not save this provider. Try again.') };
	} catch {
		return { ok: false, message: SERVER_UNREACHABLE };
	}
}

/** Save the name the picker shows for a model; an empty label restores the discovered one. */
export async function setModelLabel(dbId: string, label: string): Promise<{ ok: boolean; message?: string }> {
	try {
		const res = await fetch(`/api/settings/models/${encodeURIComponent(dbId)}?token=${t()}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ alias: label.trim() || null }),
		});
		if (res.ok) return { ok: true };
		const data = await res.json().catch(() => null);
		console.error('[models] label rejected', data?.detail);
		return { ok: false, message: plainErrorText(data?.detail, 'Could not save the label. Try again.') };
	} catch (error) {
		console.error('[models] label request failed', error);
		return { ok: false, message: SERVER_UNREACHABLE };
	}
}
