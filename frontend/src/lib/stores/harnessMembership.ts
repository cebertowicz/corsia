import { isPickerMember, modelMatchesId, type Model } from './models';
import { modelAvailability } from './modelTurn';

/** Harness identity and discovery membership have one owner across picker and Settings. */
export const HARNESSES: readonly { name: string; label: string; vendor: string }[] = [
	{ name: 'claude-code', label: 'Claude Code', vendor: 'Claude' },
	{ name: 'codex', label: 'Codex', vendor: 'Codex' },
];

const HARNESS_NAMES = new Set(HARNESSES.map((harness) => harness.name));

type SettingsCatalogRow = Record<string, unknown>;

export function isHarnessModel(row: { connection?: string; provider?: string }): boolean {
	return HARNESS_NAMES.has(row.connection ?? row.provider ?? '');
}

/** Discovery fact shared by the picker and Settings inventory. */
export function isDiscoveredHarnessModel(row: { available?: unknown }): boolean {
	return Boolean(row.available);
}

/** The catalog rows the picker can group; stale blocked-row suppression is group-local. */
export function pickerCatalogModels(all: Model[], current?: string | null): Model[] {
	return all.filter((model) => {
		if (!isPickerMember(model, current)) return false;
		if (isHarnessModel(model) && !isDiscoveredHarnessModel(model) && !modelMatchesId(model, current)) return false;
		const turn = modelAvailability(model);
		return turn.status === 'ready' || (turn.status === 'unavailable' && Boolean(turn.status_reason));
	});
}

/** Drop a blocked row these rows already offer ready under the same identity; the current selection stays. */
export function withoutStaleSiblings<T extends Model>(rows: T[], current?: string | null): T[] {
	const isReady = (model: Model) => modelAvailability(model).status === 'ready';
	const readyIdentities = new Set(rows.filter(isReady).map((model) => model.resolved_identity).filter(Boolean));
	return rows.filter((model) => isReady(model) || modelMatchesId(model, current)
		|| !model.resolved_identity || !readyIdentities.has(model.resolved_identity));
}

function settingsRowIsAlias(row: SettingsCatalogRow): boolean {
	return Boolean(row.alias_of);
}

function isSettingsHarness(row: SettingsCatalogRow, harness: string): boolean {
	return row.harness === harness || row.provider === harness;
}

/**
 * Settings shows final picker-ready rows without a current-thread exception, plus
 * real discovered rows the person hid and can re-enable. Inventory-only rows that
 * are stale, reference, or aliases never become Settings controls.
 */
export function settingsHarnessModelIds(
	settingsRows: SettingsCatalogRow[], harness: string, pickerCatalog: Model[],
): string[] {
	const settingsIds = new Set(settingsRows
		.filter((row) => typeof row.id === 'string')
		.map((row) => row.id as string));
	const pickerIds = withoutStaleSiblings(pickerCatalogModels(pickerCatalog)
		.filter((model) => model.connection === harness || model.provider === harness))
		.map((model) => model.id)
		.filter((id) => settingsIds.has(id));
	const visible = new Set(pickerIds);
	for (const row of settingsRows) {
		if (!isSettingsHarness(row, harness) || row.enabled !== 0 || !isDiscoveredHarnessModel(row)) continue;
		if (row.source === 'reference' || settingsRowIsAlias(row) || typeof row.id !== 'string') continue;
		visible.add(row.id);
	}
	return [...pickerIds, ...settingsRows
		.filter((row) => typeof row.id === 'string' && visible.has(row.id) && !pickerIds.includes(row.id))
		.map((row) => row.id as string)];
}
