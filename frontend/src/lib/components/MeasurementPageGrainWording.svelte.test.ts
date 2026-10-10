import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MeasurementPage from './MeasurementPage.svelte';
import { measurementPanels, measurementScope } from '$lib/stores/measurement';
import { measurementGrain } from '$lib/stores/measurementGrain';
import { measurementCost } from '$lib/stores/measurementCost';
import { token } from '$lib/stores/auth';
import { daysPeriod, periodsPeriod } from '../__tests__/fixtures/measurementPeriod';

type Grain = 'days' | 'weeks';
const step = (grain: Grain) => (grain === 'days' ? 1 : 7);
const COUNT = 12;
const start = '2026-08-10';
const at = (grain: Grain, i: number) => new Date(Date.parse(`${start}T00:00:00Z`) + i * step(grain) * 86_400_000).toISOString().slice(0, 10);
const last = (grain: Grain) => at(grain, COUNT - 1);
const period = (grain: Grain) => (grain === 'days' ? daysPeriod(last(grain)) : periodsPeriod(last(grain)));
const base = { proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false } as const;
const coverage = (i: number) => ({
	outcomes: 30, sized: 25, estimated_hours: 50 + i, active_hours: 0.5, unsized: 3, unknown: 1, stale: 1, conflict: 0, retracted: 0, malformed: 0,
	basis: { delivered_evidence_proxy: 25, accepted_scope_proxy: 0, author_declared: 0 }, profiles: [], comparable_reason: null, rate_reason: null,
	class_midpoint_hours: { XS: 0.1, S: 0.5, M: 2, L: 8, XL: 40, XXL: 120 }, data_start: start, comparability_before: start,
});
/** A pooled comparison window as the Cost payload serves it: named by the days it covers, with no `period` key. */
const pooled = ({ period: from, average: _average, ...figures }: ReturnType<typeof costPeriod>) => ({ from, to: from, ...figures });
const costPeriod = (day: string) => ({ period: day, dollars: 900, priced_records: 80, api_records: 100, outcomes: 30, per_outcome: 30, sized: 25, hours: 120, per_hour: 7.5, average: 7.5 });

function serve(grain: Grain) {
	return vi.fn(async (url: string) => ({ ok: true, json: async () => {
		const parsed = new URL(url, 'http://x');
		if (parsed.pathname.endsWith('/cost')) {
			const series = Array.from({ length: COUNT }, (_, i) => costPeriod(at(grain, i)));
			return {
				definition_version: 1, period: period(grain), average_periods: grain === 'days' ? 7 : 4, series, latest: pooled(series[COUNT - 1]), previous: pooled(series[COUNT - 2]), coverage: { priced_records: 80, api_records: 100 }, not_api_priced_records: 0, excluded: [],
				actual: { subscription_records: 10, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 4, complete: true, workspace_subscription: null },
			};
		}
		if (!parsed.pathname.endsWith('/series')) return {};
		const ids = parsed.searchParams.get('ids')?.split(',') ?? [];
		return {
			period: period(grain), average_periods: grain === 'days' ? 7 : 4,
			rows: ids.flatMap((series) => Array.from({ length: COUNT }, (_, i) => ({
				...base, period: at(grain, i), series, average: 0.2 + i / 100, value: series === 'estimated_human_hours_per_active_hour' ? 100 + i : 0.2 + i / 100, n: 30, proxy: series === 'estimated_human_hours_per_active_hour',
				...(series === 'estimated_human_hours_per_active_hour' ? { size: coverage(i) } : {}),
			}))),
			windows: Object.fromEntries(ids.map((series) => {
				const sized = series === 'estimated_human_hours_per_active_hour';
				const figure = (value: number) => ({ value, n: 120, ...(sized ? { size: { outcomes: 150, sized: 120, excluded: 30, estimated_hours: value * 4, active_hours: 4 } } : {}) });
				return [series, { latest: figure(sized ? 110 : 0.3), previous: figure(sized ? 100 : 0.25) }];
			})),
		};
	} }));
}

let component: Record<string, unknown> | null = null;
let host: HTMLElement;

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.unstubAllGlobals();
	measurementPanels.set({});
	measurementCost.set({ loading: true, error: null, data: null, scopeKey: null, grain: null });
	measurementGrain.set('days');
	measurementScope.set({ project: 'all', agent: null });
	token.set(null);
	window.history.replaceState({}, '', '/');
});

/** Everything a reader or a screen reader gets from the page: text, labels, titles and SVG descriptions. */
function readable(page: HTMLElement): string {
	// The four kept charts own their Days wording, and the Agents table and Where to look sections their own grain copy.
	const root = page.cloneNode(true) as HTMLElement;
	root.querySelectorAll('.kept-panels, #measurement-where-to-look, .seg').forEach((el) => el.remove());
	const attributes = [...root.querySelectorAll('[aria-label], [title], [alt]')].flatMap((el) => ['aria-label', 'title', 'alt'].map((name) => el.getAttribute(name) ?? ''));
	return [root.textContent ?? '', ...attributes].join('\n');
}

async function render(grain: Grain): Promise<void> {
	vi.stubGlobal('fetch', serve(grain));
	host = document.createElement('div');
	document.body.appendChild(host);
	window.history.replaceState({}, '', `/?project=prj-atlas&agent=none&grain=${grain}`);
	measurementGrain.set(grain);
	measurementScope.set({ project: 'prj-atlas', agent: null });
	component = mount(MeasurementPage, { target: host });
	await vi.waitFor(() => {
		flushSync();
		expect(host.querySelector('.sizing-headline .stat b')).not.toBeNull();
		expect(host.querySelector('section.cost .chart, section.cost svg')).not.toBeNull();
		expect(host.querySelectorAll('.kept-panels svg').length).toBeGreaterThan(2);
	});
}

describe('MeasurementPage wording follows the grain', () => {
	it('shows no week wording anywhere at Days, and names the moving average by its seven days', async () => {
		await render('days');
		const text = readable(host).replaceAll('7-day moving average', '');
		const leaks = [...new Set([...text.matchAll(/.{0,50}(?:week|moving average).{0,50}/gi)].map((match) => match[0].replace(/\d{4}-\d{2}-\d{2}/g, 'DATE').replace(/\d+/g, 'N')))];
		expect(leaks).toEqual([]);
		expect(readable(host)).toContain('7-day moving average');
		expect(host.querySelectorAll('.chart svg polyline.ma').length).toBeGreaterThan(2);
	});

	it('keeps week wording, and the moving average only where one is drawn, at Weeks', async () => {
		await render('weeks');
		const text = readable(host);
		expect(text).toMatch(/week/i);
		expect(text).toContain('4-week moving average');
		expect(text).not.toContain('7-day');
	});
});
