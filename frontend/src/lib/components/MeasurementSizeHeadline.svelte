<script lang="ts">
	import { formatMeasurementValue, type PanelState, type SizeCoverage } from '$lib/stores/measurement';
	import MeasurementBarChart from './MeasurementBarChart.svelte';
	import { SIZE_SERIES, grainChart, hoursPerOutcome, minutesPerOutcome } from './measurementSize';
	import { periodChange, sizeView, type RateWindow } from './measurementPeriodView';

	let { state, onRetry }: { state: PanelState; onRetry: () => void } = $props();

	const dayFormat = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' });
	/** A served ISO date as the reader sees it; the server owns which date it is. */
	const day = (iso: string) => dayFormat.format(new Date(`${iso.slice(0, 10)}T00:00:00Z`));
	/** Copy for each served ``size.comparable_reason``; the server alone decides whether a week compares. */
	const reasons = (size: SizeCoverage): Record<string, string> => ({
		outcome_definition_changed: `outcomes were counted differently before ${day(size.comparability_before)}`,
		mixed_profiles: 'outcomes this week were rated by more than one rater or rubric',
		profile_changes_in_range: 'the rater or rubric changes within this range',
	});
	/** Copy for each served ``size.rate_reason``; the code, not the counts, says why a rate is withheld. */
	const rateStatus = (size: SizeCoverage): Record<string, string> => ({
		before_data_start: `Before this estimate starts on ${day(size.data_start)}, so no rate; recorded sizes are shown as a subtotal.`,
		no_active_time: 'No active time recorded this week, so no rate.',
		no_usable_ratings: 'No usable size estimates this week, so no rate.',
		below_minimum: `Too few sized outcomes for a rate; ${formatMeasurementValue(size.estimated_hours, 'hours')} estimated hours shown as a subtotal.`,
	});
	const group = (value: number, decimals = 0) => value.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
	const oneDecimal = (value: number | null) => (value === null ? '–' : group(value, 1));

	/** The figures are the served pooled windows, the ones the top-row tile prints; the rows only draw the chart. */
	const view = $derived(sizeView(state.rows, state.period, state.windows?.[SIZE_SERIES]));
	/** Always the most recent completed week served, even when its rate is null or partial:
	 * never an older week chosen because it has a value. */
	const latest = $derived(state.rows.filter((row) => row.series === SIZE_SERIES && row.size)
		.reduce<(typeof state.rows)[number] | null>((last, row) => (!last || row.period > last.period ? row : last), null));
	const size = $derived(latest?.size ?? null);
	const current = $derived(view.current);
	const rate = $derived(current.value);
	const nowText = $derived(view.periods.length ? view.nowText : '');
	const chart = $derived(grainChart(state.period, view.points, state.averagePeriods));
	/** "+206% vs Sep 21 to Sep 27"; with no previous window, or one the series covers only in part, it reads new. */
	const vsPrevious = (figure: (w: RateWindow) => number | null, withPreviousN = false) => periodChange(view, withPreviousN, figure);
	const status = $derived.by(() => {
		if (!latest || !size || rate !== null) return '';
		return (size.rate_reason && rateStatus(size)[size.rate_reason]) || 'No rate is served for this week.';
	});
	/** Outcomes with no usable size in the window are left out of the hours, never counted as zero. */
	const excluded = $derived(current.size?.excluded ?? 0);
	/** The week's comparability is the server's ``comparable``; a reason this client does not know still says so. */
	const comparability = $derived.by(() => {
		if (!latest || latest.comparable) return '';
		const reason = size?.comparable_reason ? reasons(size)[size.comparable_reason] : undefined;
		return reason ? `Not comparable with other weeks: ${reason}.` : 'Not comparable with other weeks.';
	});
	const midpoints = $derived(size ? Object.entries(size.class_midpoint_hours).map(([name, hours], i) => `${name} ${hours}${i ? '' : ' h'}`).join(', ') : '');
</script>

<section class="card sizing-headline" aria-labelledby="sizing-title">
	<div class="sh"><h3 id="sizing-title">Work per hour of your attention</h3></div>
	{#if state.loading && !state.rows.length}<div class="skeleton" aria-label="Loading size estimate"></div>
	{:else if state.error && !state.rows.length}<div class="panel-error" role="alert">{state.error}<button onclick={onRetry}>Retry</button></div>
	{:else if !latest || !size}<p class="empty">No completed weeks in this range.</p>
	{:else}
		{#if view.periods.length}
			<p class="stat"><b>{rate === null ? '–' : rate.toFixed(0)}</b> h of work per hour you spend, {nowText}, n {group(current.n)}, {vsPrevious((w) => w.value, true)}</p>
		{:else}
			<p class="stat"><b>–</b> h of work per hour you spend</p>
		{/if}
		{#if status}<p class="nf status">{status}</p>{/if}
		{#if view.points.length && !comparability}
			<h4>Hours of work per hour you spend, {chart.rangeText}, by {chart.unit}</h4>
			<MeasurementBarChart
				points={chart.points}
				axisTitle="Hours of work per hour you spend"
				unit={chart.unit}
				averageName={chart.averageName}
				caption={`Estimated hours of work delivered divided by time you had CORSIA open; source: size-rated accepted outcomes and recorded open time; higher is better. n is size-rated accepted outcomes per ${chart.unit}.`}
				minN={20}
				minNText="fewer than 20 outcomes"
				ariaLabel={`Hours of work per hour you spend, ${chart.rangeText}`}
				tickFormat={(value, decimals) => value.toFixed(decimals)}
				valueFormat={(value) => value.toFixed(0)}
			/>
		{/if}
		<p class="nf fine">Hours of work use class midpoints ({midpoints || 'XS to XXL'}). {group(excluded)} of {group(current.size?.outcomes ?? 0)} accepted outcomes in {nowText} have no usable size rating and are left out of hours, never counted as zero. Windows with fewer than 20 size-rated outcomes read low n and are not a baseline.</p>
		{#if comparability}<p class="nf comparability">{comparability}</p>{/if}
		<div class="why">
			<h4>Why it moved, size-rated outcomes only, {nowText}</h4>
			<div class="whyrow">
				<div class="kpi sm"><div class="kl">Hours of work per size-rated outcome</div><div class="kv">{oneDecimal(hoursPerOutcome(current.size))}</div><div class="kc">{vsPrevious((w) => hoursPerOutcome(w.size))}</div></div>
				<div class="kpi sm"><div class="kl">Your minutes per size-rated outcome</div><div class="kv">{oneDecimal(minutesPerOutcome(current.size))}</div><div class="kc">{vsPrevious((w) => minutesPerOutcome(w.size))}</div></div>
			</div>
			<p class="nf fine">Work per hour you spend = hours per size-rated outcome divided by your minutes per size-rated outcome, times 60. n is {group(current.n)} size-rated outcomes.</p>
		</div>
		{#if state.error}<div class="panel-error" role="alert">{state.error}<button onclick={onRetry}>Retry</button></div>
		{:else if state.loading}<p class="nf refreshing" aria-live="polite">Refreshing…</p>{/if}
	{/if}
</section>

<style>
	.card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: 0; padding: 14px; min-width: 0; }
	.sh { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin-bottom: 6px; }
	h3 { margin: 0; color: var(--text-primary); font-size: 18px; font-weight: 600; overflow-wrap: anywhere; }
	h4 { margin: 0 0 6px; color: var(--text-secondary); font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; overflow-wrap: anywhere; }
	p { margin: 0; }
	.stat { font-size: 13px; color: var(--text-secondary); margin: 0 0 12px; overflow-wrap: anywhere; }
	.stat b { font-size: 20px; color: var(--text-primary); margin-right: 4px; }
	.nf { font-size: 12px; color: var(--text-secondary); margin: 6px 0 0; overflow-wrap: anywhere; }
	.nf.fine { font-style: italic; }
	.why { margin-top: 14px; }
	.whyrow { display: grid; grid-template-columns: 1fr; gap: 8px; }
	/* r2.7's inner cell: the light tile fill, the card line and 12px padding. */
	.kpi { background: var(--mm-tile, #f7f9fb); border: 1px solid var(--border-subtle); padding: 12px; min-width: 0; }
	.kl { font-size: 12px; color: var(--text-secondary); font-weight: 650; }
	.kv { font-size: 22px; font-weight: 750; line-height: 1.1; margin: 4px 0 2px; color: var(--text-primary); }
	.kc { font-size: 12px; color: var(--text-secondary); overflow-wrap: anywhere; }
	.skeleton { height: 48px; border-radius: var(--radius-xs); background: var(--bg-hover); }
	.empty { color: var(--text-secondary); font-size: 13px; }
	.panel-error { color: var(--red-text); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	.panel-error button { border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); background: var(--bg-primary); color: var(--text-primary); padding: 4px 8px; cursor: pointer; }
	@media (min-width: 900px) { .whyrow { grid-template-columns: 1fr 1fr; } }
</style>
