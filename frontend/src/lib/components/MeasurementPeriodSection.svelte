<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { PanelState } from '$lib/stores/measurement';
	import MeasurementBarChart from './MeasurementBarChart.svelte';
	import type { BarPoint } from './measurementBarChart';
	import { periodChange, periodView } from './measurementPeriodView';
	import { MIN_N, grainChart } from './measurementSize';

	type ChartSlot = { points: BarPoint[]; unit: 'day' | 'week'; axisTitle: string; caption: string; ariaLabel: string };

	let { id, title, state, onRetry, series, scale = 1, unitText, headline, chartTitle, axisTitle, caption, percent = false, valueFormat, tickFormat, minN, minNNoun = 'outcomes', extra, after, chart: drawChart }: {
		id: string;
		title: string;
		state: PanelState;
		onRetry: () => void;
		/** The served series this card draws, and what turns its value into the unit the reader sees. */
		series: string;
		scale?: number;
		/** What follows the figure in the headline, e.g. "minutes per outcome". */
		unitText: string;
		/** The figure as written in the headline. */
		headline: (value: number) => string;
		/** The h4 above the chart, before its date range. */
		chartTitle: string;
		axisTitle: string;
		caption: string;
		percent?: boolean;
		/** How a bar's value label reads; one decimal where the figures are small, as the render has it. */
		valueFormat?: (value: number) => string;
		/** How an axis tick reads, given the decimals its step needs. */
		tickFormat?: (value: number, decimals: number) => string;
		/**
		 * The n below which a rate's bar is hollow; by default the series' served minimum, which its change reads too. A count passes 0:
		 * its n is its value, so no bar is ever low n, and its change keeps the product minimum.
		 */
		minN?: number;
		/** What n counts, for the hollow-bar rule, e.g. "decisions". */
		minNNoun?: string;
		/** A line that belongs under the headline, e.g. a second measure of the same outcomes. */
		extra?: Snippet;
		/** Rendered under the chart, e.g. a per-agent table for the same measure. */
		after?: Snippet;
		/** Draws the chart in place of the single-series bars, on the same periods, e.g. Alignment's three shares. */
		chart?: Snippet<[ChartSlot]>;
	} = $props();

	const group = (value: number) => value.toLocaleString('en-US');
	const view = $derived(periodView(state.rows, series, scale, state.period, state.windows?.[series]));
	const chart = $derived(grainChart(state.period, view.points, state.averagePeriods));
	const value = $derived(view.current.value);
	const served = $derived(state.minimumN?.[series]);
	const minimum = $derived(minN ?? served ?? MIN_N);
	const minNText = $derived(`fewer than ${minimum} ${minNNoun}`);
	/** Copy written for weeks reads per day on Days. */
	const perUnit = (text: string) => text.replace(/\bper week\b/g, `per ${chart.unit}`);
</script>

<section class="card" aria-labelledby={`${id}-title`}>
	<div class="sh"><h3 id={`${id}-title`}>{title}</h3></div>
	{#if state.loading && !state.rows.length}<div class="skeleton" aria-label={`Loading ${title}`}></div>
	{:else if state.error && !state.rows.length}<div class="panel-error" role="alert">{state.error}<button onclick={onRetry}>Retry</button></div>
	{:else if !state.rows.length}<p class="empty">Nothing was recorded in this range.</p>
	{:else if !view.periods.length}<p class="empty">Nothing measured for this selection.</p>
	{:else}
		<p class="stat"><b>{value === null ? '–' : headline(value)}</b> {unitText}, {view.nowText}, n {group(view.current.n)}, {periodChange(view, true, undefined, minN === undefined ? served : undefined)}</p>
		{@render extra?.()}
		<h4>{chartTitle}, {chart.rangeText}, by {chart.unit}</h4>
		{#if drawChart}
			{@render drawChart({ points: chart.points, unit: chart.unit, axisTitle: perUnit(axisTitle), caption: perUnit(caption), ariaLabel: `${chartTitle}, ${chart.rangeText}` })}
		{:else}
		<MeasurementBarChart
			points={chart.points}
			axisTitle={perUnit(axisTitle)}
			unit={chart.unit}
			averageName={chart.averageName}
			caption={perUnit(caption)}
			minN={minimum}
			{minNText}
			ariaLabel={`${chartTitle}, ${chart.rangeText}`}
			tickFormat={tickFormat ?? ((tick, decimals) => (percent ? `${tick.toFixed(0)}%` : tick.toFixed(decimals)))}
			valueFormat={valueFormat ?? ((point) => point.toFixed(0))}
			percent100={percent}
		/>
		{/if}
		{@render after?.()}
		{#if state.error}<div class="panel-error" role="alert">{state.error}<button onclick={onRetry}>Retry</button></div>
		{:else if state.loading}<p class="refreshing" aria-live="polite">Refreshing…</p>{/if}
	{/if}
</section>

<style>
	.card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: 0; padding: 14px; min-width: 0; }
	.sh { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin-bottom: 6px; }
	h3 { margin: 0; color: var(--text-primary); font-size: 18px; font-weight: 700; overflow-wrap: anywhere; }
	h4 { margin: 0 0 6px; color: var(--text-secondary); font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; overflow-wrap: anywhere; }
	p { margin: 0; }
	.stat { font-size: 13px; color: var(--text-secondary); margin: 0 0 12px; overflow-wrap: anywhere; }
	.stat b { font-size: 20px; color: var(--text-primary); margin-right: 4px; }
	.skeleton { height: 48px; border-radius: var(--radius-xs); background: var(--bg-hover); }
	.empty, .refreshing { color: var(--text-secondary); font-size: 13px; }
	.panel-error { color: var(--red-text); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	.panel-error button { border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); background: var(--bg-primary); color: var(--text-primary); padding: 4px 8px; cursor: pointer; }
</style>
