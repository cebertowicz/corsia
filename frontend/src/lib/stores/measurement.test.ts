import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import {
	clearMeasurementAgent,
	filterMeasurementAgent,
	formatMeasurementValue,
	measurementPanels,
	measurementScope,
	setMeasurementProject,
	writeMeasurementScope,
	loadMeasurementPanel,
	loadMeasurementPanels,
	panelSeries,
} from './measurement';
import { token } from './auth';
import { measurementGrain } from './measurementGrain';
import { decodeSeriesRows, drawnValue } from './measurementRows';

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	measurementPanels.set({});
	measurementGrain.set('days');
	measurementScope.set({ project: 'all', agent: null });
	token.set(null);
});

describe('Measurement requests', () => {
	it('sends the selected composite scope and all three alignment series', async () => {
		token.set('test-token');
		measurementScope.set({ project: 'project-1', agent: 'agent-1' });
		const fetchMock = vi.fn(async (_url: string) => ({ ok: true, json: async () => ({ rows: [] }) }));
		vi.stubGlobal('fetch', fetchMock);
		await loadMeasurementPanel('alignment');
		const query = new URL(String(fetchMock.mock.calls[0][0]), 'http://x').searchParams;
		expect(query.get('ids')).toBe('took_recommendation_pct,answered_other_pct,answered_neither_pct');
		expect(query.get('project')).toBe('project-1');
		expect(query.get('agent')).toBe('agent-1');
		expect(query.get('grain')).toBe('days');
		expect(query.get('timezone')).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
		expect(query.has('range')).toBe(false);
		expect(query.has('scope')).toBe(false);
		expect(query.has('scope_id')).toBe(false);
		expect(query.has('from')).toBe(false);
		expect(query.has('to')).toBe(false);
	});

	it('requests the existing token metric and retains separate raw harness rows', async () => {
		const rows = ['native', 'unclassified'].map((dimension, index) => ({ period: '2026-09-21', series: 'fresh_tokens_per_outcome', value: 123456.789 + index, n: 30, dimension, proxy: index === 1, partial: false, comparable: true, break: null, thin: false }));
		const fetchMock = vi.fn(async (_url: string) => ({ ok: true, json: async () => ({ rows }) }));
		vi.stubGlobal('fetch', fetchMock);
		await loadMeasurementPanel('tokens');
		expect(new URL(String(fetchMock.mock.calls[0][0]), 'http://x').searchParams.get('ids')).toBe('fresh_tokens_per_outcome');
		expect(get(measurementPanels).tokens.rows).toEqual(rows);
	});

	it('carries the selected grain and the viewer\'s zone on every panel request', async () => {
		measurementGrain.set('weeks');
		const fetchMock = vi.fn(async (_url: string) => ({ ok: true, json: async () => ({ rows: [] }) }));
		vi.stubGlobal('fetch', fetchMock);
		await loadMeasurementPanels();
		const seriesUrls = fetchMock.mock.calls.map(([url]) => String(url));
		expect(seriesUrls.length).toBeGreaterThan(0);
		for (const url of seriesUrls) {
			const query = new URL(url, 'http://x').searchParams;
			expect(query.get('grain')).toBe('weeks');
			expect(query.get('timezone')).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
			expect(query.has('range')).toBe(false);
		}
	});

	it('keeps panel failures independent and retains the rows of the same selection', async () => {
		const loaded = { period: '2026-09-21', series: 'outcomes', value: 3, n: 3, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ rows: [loaded] }) })));
		await loadMeasurementPanel('outcomes');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })));
		await loadMeasurementPanel('outcomes');
		expect(get(measurementPanels).outcomes).toMatchObject({ loading: false, error: 'Could not load this measurement.', selection: 'days:all:none' });
		expect(get(measurementPanels).outcomes?.rows).toEqual([loaded]);
		expect(get(measurementPanels).quality).toBeUndefined();
	});

	it('drops the rows of another selection instead of carrying them into a failure', async () => {
		const loaded = { period: '2026-09-21', series: 'outcomes', value: 3, n: 3, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ rows: [loaded] }) })));
		await loadMeasurementPanel('outcomes');
		measurementScope.set({ project: 'project-2', agent: null });
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })));
		await loadMeasurementPanel('outcomes');
		expect(get(measurementPanels).outcomes).toEqual({ loading: false, rows: [], error: 'Could not load this measurement.', selection: 'days:project-2:none' });
	});

	it('loads every panel with one series request and splits its rows by panel', async () => {
		measurementScope.set({ project: 'project-1', agent: null });
		const row = (series: string, value: number) => ({ period: '2026-09-21', series, value, n: 40, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false });
		const rows = [row('outcomes', 5), row('took_recommendation_pct', 0.5), row('answered_other_pct', 0.2), row('answered_neither_pct', 0.3), row('first_try_acceptance', 0.9)];
		const fetchMock = vi.fn(async (_url: string) => ({ ok: true, json: async () => ({ rows }) }));
		vi.stubGlobal('fetch', fetchMock);
		await loadMeasurementPanels();
		expect(fetchMock).toHaveBeenCalledTimes(1);
		const query = new URL(String(fetchMock.mock.calls[0][0]), 'http://x').searchParams;
		expect(query.get('ids')?.split(',')).toEqual(Object.values(panelSeries).flat());
		expect(query.get('project')).toBe('project-1');
		expect(query.get('grain')).toBe('days');
		const panels = get(measurementPanels);
		expect(Object.keys(panels).sort()).toEqual(Object.keys(panelSeries).sort());
		expect(panels.outcomes).toEqual({ loading: false, rows: [rows[0]], error: null, selection: 'days:project-1:none' });
		expect(panels.alignment.rows).toEqual(rows.slice(1, 4));
		expect(panels['first-try'].rows).toEqual([rows[4]]);
		expect(panels.asks).toEqual({ loading: false, rows: [], error: null, selection: 'days:project-1:none' });
	});

	it('marks every panel failed when the page request fails, keeping each panel its own rows', async () => {
		const loaded = { period: '2026-09-21', series: 'outcomes', value: 3, n: 3, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ rows: [loaded] }) })));
		await loadMeasurementPanels();
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })));
		await loadMeasurementPanels();
		const panels = get(measurementPanels);
		for (const panel of Object.keys(panelSeries)) expect(panels[panel]).toMatchObject({ loading: false, error: 'Could not load this measurement.' });
		expect(panels.outcomes.rows).toEqual([loaded]);
		expect(panels.asks.rows).toEqual([]);
	});

	it('retries one panel without cancelling the page request the other panels still wait on', async () => {
		const answers: Array<{ url: string; signal: AbortSignal; answer: (rows: Array<Record<string, unknown>>) => void }> = [];
		vi.stubGlobal('fetch', vi.fn((url: string, init: { signal: AbortSignal }) => new Promise((resolve) => {
			answers.push({ url, signal: init.signal, answer: (rows) => resolve({ ok: true, json: async () => ({ rows }) }) });
		})));
		const row = (series: string, value: number) => ({ period: '2026-09-21', series, value, n: 40, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false });
		const page = loadMeasurementPanels();
		const retry = loadMeasurementPanel('outcomes');
		expect(answers).toHaveLength(2);
		expect(answers[0].signal.aborted).toBe(false);
		expect(new URL(answers[1].url, 'http://x').searchParams.get('ids')).toBe('outcomes');
		answers[1].answer([row('outcomes', 7)]);
		answers[0].answer([row('outcomes', 100), row('first_try_acceptance', 0.9)]);
		await Promise.all([page, retry]);
		expect(get(measurementPanels).outcomes.rows.map((entry) => entry.value)).toEqual([7]);
		expect(get(measurementPanels)['first-try'].rows.map((entry) => entry.value)).toEqual([0.9]);
	});

	it('cancels the shared page request once a newer page load takes every panel', async () => {
		const signals: AbortSignal[] = [];
		vi.stubGlobal('fetch', vi.fn((_url: string, init: { signal: AbortSignal }) => {
			signals.push(init.signal);
			return new Promise(() => {});
		}));
		void loadMeasurementPanels();
		measurementScope.set({ project: 'project-2', agent: null });
		void loadMeasurementPanels();
		expect(signals).toHaveLength(2);
		expect(signals[0].aborted).toBe(true);
		expect(signals[1].aborted).toBe(false);
	});

	it('ignores an older selection that answers after the newer one', async () => {
		token.set('test-token');
		const answers: Array<(rows: Array<Record<string, unknown>>) => void> = [];
		vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => {
			answers.push((rows) => resolve({ ok: true, json: async () => ({ rows }) }));
		})));
		measurementScope.set({ project: 'all', agent: null });
		const first = loadMeasurementPanel('outcomes');
		measurementScope.set({ project: 'project-2', agent: null });
		const second = loadMeasurementPanel('outcomes');
		const row = (period: string, value: number) => ({ period, series: 'outcomes', value, n: 40, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false });
		answers[1]([row('2026-09-21', 20)]);
		answers[0]([row('2026-09-21', 100)]);
		await Promise.all([first, second]);
		expect(get(measurementPanels).outcomes?.rows.map((entry) => entry.value)).toEqual([20]);
	});
});

describe('The served moving average', () => {
	const served = (average?: unknown) => ({ period: '2026-09-21', series: 'outcomes', value: 4, n: 4, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false, ...(average === undefined ? {} : { average }) });

	it('keeps a number or null on the row, and reads an absent one as no average, never zero', () => {
		const [withAverage, none, absent] = decodeSeriesRows([served(2.5), served(null), served()] as never);
		expect(withAverage.average).toBe(2.5);
		expect(none.average).toBeNull();
		expect(absent.average).toBeUndefined();
	});

	it('rejects an average that is not a finite number', () => {
		for (const bad of ['2.5', Number.NaN, Number.POSITIVE_INFINITY, {}, true]) expect(() => decodeSeriesRows([served(bad)] as never)).toThrow('measurement average shape');
	});

	it('keeps a raw value on a withheld row, and rejects one that is not a number', () => {
		const [low] = decodeSeriesRows([{ ...served(), value: null, n: 5, raw_value: 0.4 }] as never);
		expect(low).toMatchObject({ value: null, raw_value: 0.4 });
		expect(drawnValue(low)).toBe(0.4);
		expect(drawnValue({ value: null, raw_value: 0.4, n: 0 })).toBeNull();
		expect(drawnValue({ value: 2, raw_value: 3, n: 5 })).toBe(2);
		expect(() => decodeSeriesRows([{ ...served(), raw_value: '0.4' }] as never)).toThrow('measurement raw value shape');
	});

	it('fails the panel load with its error rather than drawing a line from a malformed average', async () => {
		token.set('test-token');
		measurementScope.set({ project: 'all', agent: null });
		vi.stubGlobal('fetch', vi.fn(async (_url: string) => ({ ok: true, json: async () => ({ rows: [served('x')] }) })));
		await loadMeasurementPanel('outcomes');
		expect(get(measurementPanels).outcomes).toMatchObject({ loading: false, error: 'Could not load this measurement.' });
	});

	it('serves the average with the rows of a panel', async () => {
		token.set('test-token');
		measurementScope.set({ project: 'all', agent: null });
		vi.stubGlobal('fetch', vi.fn(async (_url: string) => ({ ok: true, json: async () => ({ rows: [served(3), served(null)] }) })));
		await loadMeasurementPanel('outcomes');
		expect(get(measurementPanels).outcomes.rows.map((row) => row.average)).toEqual([3, null]);
	});
});

describe('Measurement scope selection', () => {
	it('drops an agent that is not in the newly selected project', () => {
		measurementScope.set({ project: 'one', agent: 'agent-a' });
		setMeasurementProject('two', new Map([['agent-a', 'one'], ['agent-b', 'two']]));
		expect(get(measurementScope)).toEqual({ project: 'two', agent: null });
		setMeasurementProject('two', new Map([['agent-a', 'one'], ['agent-b', 'two']]));
		measurementScope.set({ project: 'two', agent: 'agent-b' });
		setMeasurementProject('two', new Map([['agent-a', 'one'], ['agent-b', 'two']]));
		expect(get(measurementScope)).toEqual({ project: 'two', agent: 'agent-b' });
	});

	it('keeps the agent when the selection widens to all work', () => {
		measurementScope.set({ project: 'one', agent: 'agent-a' });
		setMeasurementProject('all', new Map([['agent-a', 'one'], ['agent-b', 'two']]));
		expect(get(measurementScope)).toEqual({ project: 'all', agent: 'agent-a' });
	});

	it('removes only the agent when the chip is cleared', () => {
		measurementScope.set({ project: 'one', agent: 'agent-a' });
		clearMeasurementAgent();
		expect(get(measurementScope)).toEqual({ project: 'one', agent: null });
	});

});

describe('formatMeasurementValue', () => {
	it('formats ratios, hours, counts, and percents for readable dashboard values', () => {
		expect(formatMeasurementValue(0.131233)).toBe('0.13');
		expect(formatMeasurementValue(1.236)).toBe('1.24');
		expect(formatMeasurementValue(0.004)).toBe('0.004');
		expect(formatMeasurementValue(0)).toBe('0');
		expect(formatMeasurementValue(4.56, 'hours')).toBe('4.6');
		expect(formatMeasurementValue(3600.4, 'hours')).toBe('3,600.4');
		expect(formatMeasurementValue(48, 'hours')).toBe('48.0');
		expect(formatMeasurementValue(1.7999999999999998, 'minutes')).toBe('1.8');
		expect(formatMeasurementValue(0.03, 'minutes')).toBe('0.03');
		expect(formatMeasurementValue(0, 'hours')).toBe('0.0');
		expect(formatMeasurementValue(0.01, 'hours')).toBe('0.01');
		expect(formatMeasurementValue(1234, 'count')).toBe('1,234');
		expect(formatMeasurementValue(0.7795, 'percent')).toBe('78%');
	});

	it('never rounds away a digit the served ratio carries', () => {
		expect(formatMeasurementValue(762)).toBe('762');
		expect(formatMeasurementValue(123.4)).toBe('123.4');
		expect(formatMeasurementValue(-123.4)).toBe('-123.4');
		expect(formatMeasurementValue(-0.0025)).toBe('-0.0025');
		expect(formatMeasurementValue(0.004)).toBe('0.004');
		expect(formatMeasurementValue(-0.004)).toBe('-0.004');
	});

	it('renders an unmeasured value as nothing rather than zero', () => {
		expect(formatMeasurementValue(null)).toBe('');
		expect(formatMeasurementValue(null, 'count')).toBe('');
	});
});

describe('The one composite scope writer', () => {
	it('applies a row\'s agent and its project as one pair', () => {
		filterMeasurementAgent('agent-1', 'project-1');
		expect(get(measurementScope)).toEqual({ project: 'project-1', agent: 'agent-1' });
		filterMeasurementAgent('agent-2', null);
		expect(get(measurementScope)).toEqual({ project: 'all', agent: 'agent-2' });
	});

	it('drops an agent that is not in the project the reader switched to, and never resurrects it', () => {
		const projects = new Map([['agent-1', 'project-1'], ['agent-2', 'project-2']]);
		filterMeasurementAgent('agent-1', 'project-1');
		setMeasurementProject('project-2', projects);
		expect(get(measurementScope)).toEqual({ project: 'project-2', agent: null });
		// Coming back to the project does not silently re-apply a filter the reader lost.
		setMeasurementProject('project-1', projects);
		expect(get(measurementScope)).toEqual({ project: 'project-1', agent: null });
		filterMeasurementAgent('agent-1', 'project-1');
		setMeasurementProject('all', projects);
		expect(get(measurementScope)).toEqual({ project: 'all', agent: 'agent-1' });
	});

	it('refuses a pair the writer cannot make consistent, and clearing leaves the project alone', () => {
		writeMeasurementScope({ project: 'project-2', agent: 'agent-1' }, (agent) => (agent === 'agent-1' ? 'project-1' : null));
		expect(get(measurementScope)).toEqual({ project: 'project-2', agent: null });
		filterMeasurementAgent('agent-1', 'project-1');
		clearMeasurementAgent();
		expect(get(measurementScope)).toEqual({ project: 'project-1', agent: null });
	});
});
