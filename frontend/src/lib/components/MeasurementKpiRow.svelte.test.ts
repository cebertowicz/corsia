import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { tick } from 'svelte';
import MeasurementKpiRow from './MeasurementKpiRow.svelte';
import MeasurementSizeHeadline from './MeasurementSizeHeadline.svelte';
import type { Kpi, KpiPeriod, KeyFiguresState } from '$lib/stores/keyFigures';
import { measurementCost } from '$lib/stores/measurementCost';
import { measurementPanels, type MeasurementRow, type PanelState, type SeriesWindows } from '$lib/stores/measurement';
import { decodePeriod } from '$lib/stores/measurementPeriod';
import { measurementGrain } from '$lib/stores/measurementGrain';

const figure = (value: number, unknown_records = 0) => ({ value, unknown_records });
const period = (extra: Partial<KpiPeriod> = {}): KpiPeriod => ({
	start: 's', end: 'e', from: '2026-09-28', to: '2026-10-04',
	tokens: { total: figure(34_590_000_000), input: figure(526_000_000), cache_write: figure(278_000_000), cache_read: figure(33_660_000_000), output: figure(119_000_000) },
	...extra,
});
const before = period({ from: '2026-09-21', to: '2026-09-27', tokens: { total: figure(15_300_000_000), input: figure(1), cache_write: figure(1), cache_read: figure(1), output: figure(1) } });
const state = (kpi: Kpi | null, extra: Partial<KeyFiguresState> = {}): KeyFiguresState => ({ loading: false, kpi, error: null, selection: 'x', ...extra });

const span = { latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } };
const windowsBody = (extra: Record<string, unknown> = {}) => ({
	period: { grain: 'days', timezone: 'America/Los_Angeles', current: '2026-10-04', range: { from: '2026-09-21', to: '2026-10-04' }, ...span },
	windows: {
		estimated_human_hours_per_active_hour: { latest: { value: 92.4, n: 1349 }, previous: { value: 30.2, n: 451 } },
		first_try_acceptance: { latest: { value: 0.89, n: 1377 }, previous: { value: 0.85, n: 451 } },
		active_hours_per_outcome: { latest: { value: 0.03, n: 1377 }, previous: { value: 0.097, n: 451 } },
		...extra,
	},
});
const costPeriod = (per_hour: number | null, sized: number) => ({ from: '2026-09-28', to: '2026-10-04', dollars: per_hour === null ? null : per_hour * 1_000, priced_records: 1, api_records: 1, outcomes: sized, per_outcome: 1, sized, hours: 1_000, per_hour });
const costData = (previous: ReturnType<typeof costPeriod> | null) => ({
	definition_version: 1 as const, period: { grain: 'days' as const, timezone: 'x', current: '2026-10-04', range: span.latest, ...span },
	// One served day per day of both windows: a previous window the series covers in full is compared, not read as new.
	series: Array.from({ length: 14 }, (_, i) => ({ ...costPeriod(1.49, 1187), period: new Date(Date.parse('2026-09-21T00:00:00Z') + i * 86_400_000).toISOString().slice(0, 10), average: null })), latest: costPeriod(1.49, 1187), previous, coverage: { priced_records: 1, api_records: 1 }, not_api_priced_records: 0, excluded: [],
	actual: { subscription_records: 0, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 0, complete: true, workspace_subscription: null },
});

const days = (from: string): string[] => Array.from({ length: 7 }, (_, i) => new Date(Date.parse(`${from}T00:00:00Z`) + i * 86_400_000).toISOString().slice(0, 10));
const rowBase = { proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false, raw_value: null, average: null } as const;
const sizeBasis = { outcomes: 0, sized: 0, estimated_hours: 0, active_hours: 1, unsized: 0, unknown: 0, stale: 0, conflict: 0, retracted: 0, malformed: 0, basis: { delivered_evidence_proxy: 0, accepted_scope_proxy: 0, author_declared: 0 }, profiles: [], comparable_reason: null, rate_reason: null, class_midpoint_hours: { XS: 0.1, S: 0.5, M: 2, L: 8, XL: 40, XXL: 120 } };
/** The daily rows behind a served window pair: seven days a window, the window's n split across them, so the section headline and the tile read the same rows. */
function seriesRows(id: string, windows: SeriesWindows, dataStart = '2026-09-01'): MeasurementRow[] {
	return ([['2026-09-21', windows.previous], ['2026-09-28', windows.latest]] as const).flatMap(([from, figure]) => days(from).map((day, i) => {
		const n = Math.floor(figure.n / 7) + (i === 0 ? figure.n % 7 : 0);
		const size = id === 'estimated_human_hours_per_active_hour'
			? { ...sizeBasis, outcomes: n, sized: figure.value === null ? 0 : n, estimated_hours: figure.value ?? 0, data_start: `${dataStart}T00:00:00Z`, comparability_before: dataStart }
			: undefined;
		return { ...rowBase, period: day, series: id, value: figure.value, n, ...(size ? { size } : {}) } as unknown as MeasurementRow;
	}));
}

/** The panels the row reads, each carrying the windows block its own section headlines and the rows behind it. */
function seedPanels(served: ReturnType<typeof windowsBody>, overrides: Record<string, Partial<PanelState>> = {}): void {
	const panel = (name: string, id: string): PanelState => {
		const windows = (served.windows as Record<string, SeriesWindows>)[id];
		return { loading: false, rows: seriesRows(id, windows), error: null, selection: 'x', period: decodePeriod(served.period), windows: { [id]: windows }, ...overrides[name] };
	};
	measurementPanels.set({ size: panel('size', 'estimated_human_hours_per_active_hour'), 'first-try': panel('first-try', 'first_try_acceptance'), attention: panel('attention', 'active_hours_per_outcome') });
}
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let body: ReturnType<typeof windowsBody>;
beforeEach(() => {
	body = windowsBody();
	measurementGrain.set('days');
	measurementCost.set({ loading: false, error: null, data: costData(costPeriod(1.55, 399)), scopeKey: 'all:none', grain: 'days' });
});
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); });
async function render(figures: KeyFiguresState, onRetry = vi.fn()): Promise<HTMLElement> {
	host = document.createElement('div'); document.body.append(host);
	seedPanels(body);
	component = mount(MeasurementKpiRow, { target: host, props: { state: figures, onRetry } });
	flushSync(); await tick(); return host;
}
const tile = (id: string) => host.querySelector(`[data-kpi="${id}"]`)!.textContent!.replace(/\s+/g, ' ').trim();
/** The size section's own headline, mounted from the same panel state the tile reads. */
function sizeHeadline(): string {
	const target = document.createElement('div'); host.append(target);
	let panel: PanelState | undefined;
	measurementPanels.subscribe((states) => { panel = states.size; })();
	mount(MeasurementSizeHeadline, { target, props: { state: panel!, onRetry: vi.fn() } });
	flushSync();
	return target.querySelector('.stat')!.textContent!.replace(/\s+/g, ' ').trim();
}

describe('MeasurementKpiRow', () => {
	it('shows the five tiles in order, each a number and unit with its window, n and change', async () => {
		await render(state({ current: period(), previous: before }));
		expect([...host.querySelectorAll('[data-kpi]')].map((node) => (node as HTMLElement).dataset.kpi)).toEqual(['work', 'first-try', 'cost', 'minutes', 'tokens']);
		expect(tile('work')).toBe('Hours of work per hour of your attention 92 h Sep 28 to Oct 4, n 1,349, +206% vs Sep 21 to Sep 27 (n 451)');
		expect(tile('first-try')).toBe('First-try rate (agent-graded) 89% Sep 28 to Oct 4, n 1,377, +5% vs Sep 21 to Sep 27 (n 451)');
		expect(tile('cost')).toBe('Cost per hour of delivered work $1.49 Sep 28 to Oct 4, n 1,187, -4% vs Sep 21 to Sep 27 (n 399)');
		expect(tile('minutes')).toBe('Your minutes per outcome 1.8 min Sep 28 to Oct 4, n 1,377, -69% vs Sep 21 to Sep 27 (n 451)');
		expect(tile('tokens')).toBe('Tokens 34.6B Sep 28 to Oct 4, +126% vs Sep 21 to Sep 27 Uncached input526MCache write278MCache read33.7BOutput119M');
		expect(host.querySelector('h2')).toBeNull();
	});

	it('reads new when a previous window has no value or too few outcomes, and withholds a value under the minimum n', async () => {
		body = windowsBody({
			estimated_human_hours_per_active_hour: { latest: { value: 53.2, n: 2317 }, previous: { value: null, n: 450 } },
			first_try_acceptance: { latest: { value: null, n: 12 }, previous: { value: 0.8, n: 451 } },
		});
		measurementCost.update((s) => ({ ...s, data: costData(null) }));
		await render(state({ current: period(), previous: period({ from: '2026-09-21', to: '2026-09-27', tokens: { total: figure(0), input: figure(0), cache_write: figure(0), cache_read: figure(0), output: figure(0) } }) }));
		expect(tile('work')).toContain('53 h Sep 28 to Oct 4, n 2,317, new');
		expect(tile('first-try')).toContain('– Sep 28 to Oct 4, n 12, n/a vs Sep 21 to Sep 27 (n 451)');
		expect(tile('cost')).toContain('$1.49 Sep 28 to Oct 4, n 1,187, new');
		expect(tile('tokens')).toContain('Sep 28 to Oct 4, new');
	});

	it('reads new on the tokens tile when the previous window began before tokens were fully recorded, never a percentage over a partial base', async () => {
		await render(state({ current: period(), previous: { ...before, tokens_recorded: false } }));
		expect(tile('tokens')).toContain('Sep 28 to Oct 4, new vs Sep 21 to Sep 27');
		expect(tile('tokens')).not.toContain('%');
	});

	it('reads new on the size tile when the instrument began inside the previous window, as its section headline does', async () => {
		const size = 'estimated_human_hours_per_active_hour';
		const windows = (body.windows as Record<string, SeriesWindows>)[size];
		// The server serves no day before the data start, so the previous window draws four of its seven days.
		const began = seriesRows(size, windows, '2026-09-24').filter((row) => row.period >= '2026-09-24');
		const panel = { rows: began };
		host = document.createElement('div'); document.body.append(host);
		seedPanels(body, { size: panel });
		component = mount(MeasurementKpiRow, { target: host, props: { state: state({ current: period(), previous: before }), onRetry: vi.fn() } });
		flushSync(); await tick();
		expect(tile('work')).toContain('92 h Sep 28 to Oct 4, n 1,349, new vs Sep 21 to Sep 27 (n 451)');
		expect(sizeHeadline()).toBe('92 h of work per hour you spend, Sep 28 to Oct 4, n 1,349, new vs Sep 21 to Sep 27 (n 451)');
		expect(tile('first-try')).toContain('+5% vs Sep 21 to Sep 27 (n 451)');
	});

	it('prints the figure its section headline prints when a break falls inside the latest window, not a second pooled figure', async () => {
		const size = 'estimated_human_hours_per_active_hour';
		const windows = (body.windows as Record<string, SeriesWindows>)[size];
		const rows = seriesRows(size, windows).map((row) => (row.period === '2026-10-01' ? { ...row, comparable: false } : row));
		// The server withholds the rate of a window under the minimum n; the tile prints that, not a rate of its own.
		measurementCost.update((s) => ({ ...s, data: { ...costData(null), latest: costPeriod(null, 12) } }));
		host = document.createElement('div'); document.body.append(host);
		seedPanels(body, { size: { rows, windows: { [size]: { latest: { value: 40, n: 1349 }, previous: windows.previous } } } });
		component = mount(MeasurementKpiRow, { target: host, props: { state: state({ current: period(), previous: before }), onRetry: vi.fn() } });
		flushSync(); await tick();
		// The tile, the section headline and the served window agree: one figure, never a second pool of the drawn days.
		expect(tile('work')).toContain('40 h Sep 28 to Oct 4, n 1,349, new');
		expect(sizeHeadline()).toBe('40 h of work per hour you spend, Sep 28 to Oct 4, n 1,349, new');
		expect(tile('cost')).toContain('– Sep 28 to Oct 4, n 12, ');
	});

	it('reads every token figure on the tile to the same three significant digits', async () => {
		await render(state({ current: period({ tokens: { total: figure(14_930_000), input: figure(1_291_000), cache_write: figure(456_789), cache_read: figure(13_170_000), output: figure(12_345) } }), previous: before }));
		const figures = [host.querySelector('[data-kpi="tokens"] strong')!, ...host.querySelectorAll('[data-kpi="tokens"] dd')].map((node) => node.textContent);
		expect(figures).toEqual(['14.9M', '1.29M', '457k', '13.2M', '12.3k']);
	});

	it('marks a token figure some calls did not record as a lower bound', async () => {
		await render(state({ current: period({ tokens: { total: figure(1500, 2), input: figure(800, 3), cache_write: figure(0, 3), cache_read: figure(0), output: figure(700, 1) } }), previous: before }));
		expect(tile('tokens')).toContain('1.5k+');
		expect(tile('tokens')).toContain('Uncached input800+Cache write0+Cache read0Output700+');
	});

	it('shows a placeholder per tile while loading and one retry on a failed source', async () => {
		measurementCost.set({ loading: true, error: null, data: null, scopeKey: null, grain: null });
		const retry = vi.fn();
		const view = await render(state(null, { error: "Couldn't load today's figures." }), retry);
		seedPanels(body); flushSync(); await tick();
		expect(host.querySelector('[data-kpi="cost"] [aria-label^="Loading"]')).not.toBeNull();
		expect(host.querySelectorAll('[role="alert"]')).toHaveLength(1);
		host.querySelector<HTMLButtonElement>('[data-kpi="tokens"] .error button')!.click();
		expect(retry).toHaveBeenCalledOnce();
		expect(tile('work')).toContain('92 h');
	});

	it('has a tile follow its own section panel: its error shows a retry that reloads that panel', async () => {
		const fetched = vi.fn().mockResolvedValue({ ok: false });
		vi.stubGlobal('fetch', fetched);
		await render(state({ current: period(), previous: before }));
		seedPanels(body, { 'first-try': { error: 'Could not load this measurement.', windows: undefined, period: undefined } });
		flushSync(); await tick();
		expect(tile('work')).toContain('92 h');
		expect(host.querySelectorAll('[role="alert"]')).toHaveLength(1);
		host.querySelector<HTMLButtonElement>('[data-kpi="first-try"] .error button')!.click();
		expect(String(fetched.mock.calls[0][0])).toContain('ids=first_try_acceptance');
		vi.unstubAllGlobals();
	});
});
