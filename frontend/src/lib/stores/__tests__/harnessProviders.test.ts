import { afterEach, describe, expect, it, vi } from 'vitest';
import { harnessProviderModels, harnessSummaries, setModelLabel } from '../harnessProviders';
import { type Model } from '../models';
import { pickerCatalogModels } from '../harnessMembership';
import { groupModels } from '../modelGroups';

const row = (over: Record<string, unknown>) => ({
	id: 'codex/gpt-5.6-sol', harness: 'codex', provider: 'codex', display_name: 'GPT-5.6-Sol',
	alias: null, enabled: 1, available: 1, context_window: 262144, ...over,
});
const pickerRow = (over: Partial<Model> = {}): Model => ({
	id: 'codex/gpt-5.6-sol', name: 'Codex · GPT-5.6-Sol', display_name: 'GPT-5.6-Sol',
	connection: 'codex', provider: 'codex', size: '', backend: 'codex', speed: '',
	status: 'ready', available: true, litellm_model: 'codex/gpt-5.6-sol', ...over,
});

describe('harnessSummaries', () => {
	it('counts available rows and drops a harness with none', () => {
		const catalog = [pickerRow(), pickerRow({ id: 'codex/gpt-6-sol', status: 'unavailable' })];
		expect(harnessSummaries([row({ id: 'codex/gpt-5.6-sol' }), row({ id: 'codex/gpt-6-sol' })], catalog))
			.toEqual([{ name: 'codex', label: 'Codex', enabled: true, total: 1, ready: 1 }]);
	});

	it('keeps a turned-off harness listed as off so it can be turned back on', () => {
		const catalog = [pickerRow({ status: 'unavailable', status_reason: 'provider_disabled' })];
		expect(harnessSummaries([row({ provider_enabled: false })], catalog))
			.toEqual([{ name: 'codex', label: 'Codex', enabled: false, total: 1, ready: 0 }]);
	});
});

describe('harnessProviderModels', () => {
	it('uses the picker predicate for the source-derived Settings matrix', () => {
		// Inferred from `claude_unlisted`: accepted aliases are available/enabled,
		// but expose alias_of only in `/api/models`; reference, retired and hidden
		// rows are Settings-returnable yet absent from that picker projection.
		const settingsRows = [
			row({ id: 'claude-code/opus', harness: 'claude-code', provider: 'claude-code', enabled: 1, available: 1 }),
			row({ id: 'claude-code/opus[1m]', harness: 'claude-code', provider: 'claude-code', enabled: 1, available: 1 }),
			row({ id: 'claude-code/fable[1m]', harness: 'claude-code', provider: 'claude-code', enabled: 1, available: 1 }),
			row({ id: 'claude-code/reference', harness: 'claude-code', provider: 'claude-code', source: 'reference', enabled: 0, available: 0 }),
			row({ id: 'claude-code/retired', harness: 'claude-code', provider: 'claude-code', enabled: 1, available: 0 }),
			row({ id: 'claude-code/limited', harness: 'claude-code', provider: 'claude-code', enabled: 1, available: 1 }),
			row({ id: 'claude-code/hidden', harness: 'claude-code', provider: 'claude-code', enabled: 0, available: 1 }),
		];
		const catalog = [
			pickerRow({ id: 'claude-code/opus', connection: 'claude-code', provider: 'claude-code' }),
			pickerRow({ id: 'claude-code/opus[1m]', connection: 'claude-code', provider: 'claude-code', alias_of: 'claude-code/opus' }),
			pickerRow({ id: 'claude-code/fable[1m]', connection: 'claude-code', provider: 'claude-code', alias_of: 'claude-code/fable' }),
			pickerRow({ id: 'claude-code/retired', connection: 'claude-code', provider: 'claude-code', status: 'unavailable', available: false }),
			pickerRow({ id: 'claude-code/limited', connection: 'claude-code', provider: 'claude-code', status: 'unavailable', status_reason: 'usage_limited' }),
		];
		const expected = pickerCatalogModels(catalog).filter((model) => model.connection === 'claude-code').map((model) => model.id);
		expect(harnessProviderModels(settingsRows, 'claude-code', catalog).map((model) => model.id))
			.toEqual([...expected, 'claude-code/hidden']);
	});

	it('uses the same stale-sibling-suppressed membership for the card count and expanded list', () => {
		const settingsRows = [
			row({ id: 'claude-code/opus', harness: 'claude-code', provider: 'claude-code' }),
			row({ id: 'claude-code/opus-stale', harness: 'claude-code', provider: 'claude-code' }),
			row({ id: 'claude-code/sonnet-limited', harness: 'claude-code', provider: 'claude-code' }),
		];
		const catalog = [
			pickerRow({ id: 'claude-code/opus', connection: 'claude-code', provider: 'claude-code', resolved_identity: 'claude-opus-5-5' }),
			pickerRow({ id: 'claude-code/opus-stale', connection: 'claude-code', provider: 'claude-code', resolved_identity: 'claude-opus-5-5', status: 'unavailable', status_reason: 'usage_limited' }),
			pickerRow({ id: 'claude-code/sonnet-limited', connection: 'claude-code', provider: 'claude-code', resolved_identity: 'claude-sonnet-5-5', status: 'unavailable', status_reason: 'usage_limited' }),
		];
		const [group] = groupModels(catalog);
		const list = harnessProviderModels(settingsRows, 'claude-code', catalog);
		const [summary] = harnessSummaries(settingsRows, catalog);
		expect(list.map((model) => model.id)).toEqual(group.rows.map((model) => model.id));
		expect(summary.total).toBe(list.length);
		expect(group.rows.map((model) => model.id)).toEqual(['claude-code/opus', 'claude-code/sonnet-limited']);
	});

	it('skips a picker row missing from the Settings snapshot instead of throwing', () => {
		expect(() => harnessProviderModels([], 'codex', [pickerRow()])).not.toThrow();
		expect(harnessProviderModels([], 'codex', [pickerRow()])).toEqual([]);
	});

	it('keeps a discovered disabled model in Settings so it can be re-enabled, without reopening stale or alias rows', () => {
		const settingsRows = [
			row({ id: 'codex/gpt-6.1-sol', enabled: 1, available: 1 }),
			row({ id: 'codex/gpt-6.1-terra', enabled: 0, available: 1 }),
			row({ id: 'codex/gpt-6.1-terra[1m]', enabled: 0, available: 1, alias_of: 'codex/gpt-6.1-terra' }),
			row({ id: 'codex/stale', enabled: 0, available: 0 }),
			row({ id: 'codex/reference', enabled: 0, available: 1, source: 'reference' }),
		];
		const pickerCatalog = [pickerRow({ id: 'codex/gpt-6.1-sol' })];
		expect(harnessProviderModels(settingsRows, 'codex', pickerCatalog).map((model) => [model.id, model.state]))
			.toEqual([['codex/gpt-6.1-sol', 'loaded'], ['codex/gpt-6.1-terra', 'default']]);
	});

	it('counts only ready picker rows as ready', () => {
		const catalog = [
			pickerRow(),
			pickerRow({ id: 'codex/limited', status: 'unavailable', status_reason: 'usage_limited', available: true }),
		];
		expect(harnessSummaries([row({}), row({ id: 'codex/limited' })], catalog))
			.toEqual([{ name: 'codex', label: 'Codex', enabled: true, total: 2, ready: 1 }]);
	});

	it('shows the label over the discovered name and keeps both', () => {
		const [m] = harnessProviderModels([row({ alias: 'Sol' })], 'codex', [pickerRow()]);
		expect(m).toMatchObject({ id: 'codex/gpt-5.6-sol', dbId: 'codex/gpt-5.6-sol', name: 'Sol', alias: 'Sol', discoveredName: 'GPT-5.6-Sol' });
	});

	it('shows the provider and checkpoint windows runtime enforces', () => {
		const [model] = harnessProviderModels([row({ context_window: 1_000_000, provider_context_length: 1_000_000, context_length: 262_144, context_window_override: 262_144 })], 'codex', [pickerRow()]);
		expect(model).toMatchObject({ meta: '1M context · checkpoints at 262k', contextLength: 262_144, providerContextLength: 1_000_000 });
	});

	it('shows the CORSIA default checkpoint when nothing is saved', () => {
		const [model] = harnessProviderModels([row({ context_window: 1_000_000, provider_context_length: 1_000_000, context_length: 262_144, context_window_override: null, context_window_source: 'default' })], 'codex', [pickerRow()]);
		expect(model).toMatchObject({ meta: '1M context · checkpoints at 262k' });
	});

	it('shows no checkpoint when the provider window is the one in force', () => {
		const [model] = harnessProviderModels([row({ context_window: 200_000, provider_context_length: 200_000, context_length: 200_000, context_window_override: null, context_window_source: 'provider' })], 'codex', [pickerRow()]);
		expect(model).toMatchObject({ meta: '200k context' });
	});
});

describe('setModelLabel', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('encodes the row id and clears the label when empty', async () => {
		const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
		vi.stubGlobal('fetch', fetchMock);
		expect(await setModelLabel('claude-code/opus[1m]', '   ')).toEqual({ ok: true });
		const [url, init] = fetchMock.mock.calls[0];
		expect(url).toContain('/api/settings/models/claude-code%2Fopus%5B1m%5D?');
		expect(JSON.parse(init.body)).toEqual({ alias: null });
	});
});
