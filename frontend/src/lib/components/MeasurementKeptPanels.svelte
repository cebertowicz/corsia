<script lang="ts">
	import type { PanelState } from '$lib/stores/measurement';
	import MeasurementAlignmentChart from './MeasurementAlignmentChart.svelte';
	import MeasurementPeriodSection from './MeasurementPeriodSection.svelte';

	let { outcomes, alignment, asks, rejections, tokens, onRetry }: {
		outcomes: PanelState;
		alignment: PanelState;
		asks: PanelState;
		rejections: PanelState;
		tokens: PanelState;
		onRetry: (panel: string) => void;
	} = $props();

	const group = (value: number) => Math.round(value).toLocaleString('en-US');
</script>

<div class="kept-panels">
<!-- The kept measures, drawn as the same single-series bar charts as the rest of the page; Alignment stacks its three answer shares. Fresh tokens are shown in thousands. -->
<MeasurementPeriodSection
	id="outcomes"
	title="Outcomes"
	state={outcomes}
	onRetry={() => onRetry('outcomes')}
	series="outcomes"
	unitText="accepted outcomes"
	headline={group}
	chartTitle="Accepted outcomes"
	axisTitle="Accepted outcomes per week"
	caption="Accepted outcomes per week; source: accepted outcomes; a count, not scored good or bad. n is the outcomes counted, so it equals the value."
	minN={0}
	valueFormat={group}
	tickFormat={(tick) => group(tick)}
/>
<MeasurementPeriodSection
	id="asks"
	title="Decisions and tasks for you per outcome"
	state={asks}
	onRetry={() => onRetry('asks')}
	series="asks_per_outcome"
	unitText="decisions and tasks per outcome"
	headline={(value) => value.toFixed(2)}
	chartTitle="Decisions and tasks for you per accepted outcome"
	axisTitle="Decisions and tasks for you per accepted outcome"
	caption="Decisions raised to you and tasks waiting on you, per accepted outcome; lower is better. n is accepted outcomes per week."
	valueFormat={(value) => value.toFixed(2)}
/>
<MeasurementPeriodSection
	id="alignment"
	title="Alignment"
	state={alignment}
	onRetry={() => onRetry('alignment')}
	series="took_recommendation_pct"
	scale={100}
	unitText="took the agent's recommendation"
	headline={(value) => `${value.toFixed(0)}%`}
	chartTitle="Answered decisions by answer"
	axisTitle="Percent of answered decisions"
	caption="Of the decisions you answered, the share that took the agent's recommendation, chose Other, or chose neither; source: your answers; it is not scored good or bad. n is decisions answered per week."
	minNNoun="decisions"
	percent
>
	{#snippet chart({ points, unit, axisTitle, caption, ariaLabel })}
		<MeasurementAlignmentChart rows={alignment.rows} {points} {unit} {axisTitle} {caption} {ariaLabel} />
	{/snippet}
</MeasurementPeriodSection>
<MeasurementPeriodSection
	id="rejections"
	title="Rejections per outcome"
	state={rejections}
	onRetry={() => onRetry('rejections')}
	series="rejections_per_outcome"
	unitText="rejections per outcome"
	headline={(value) => value.toFixed(2)}
	chartTitle="Review rejections per accepted outcome"
	axisTitle="Review rejections per accepted outcome"
	caption="Review rejections divided by accepted outcomes; source: review decisions and accepted outcomes; lower is better. n is accepted outcomes per week."
	valueFormat={(value) => value.toFixed(2)}
/>
<MeasurementPeriodSection
	id="tokens"
	title="Fresh tokens per outcome"
	state={tokens}
	onRetry={() => onRetry('tokens')}
	series="fresh_tokens_per_outcome"
	scale={0.001}
	unitText="fresh tokens per outcome"
	headline={(value) => `${group(value)}k`}
	chartTitle="Fresh tokens per outcome, in thousands"
	axisTitle="Thousands of fresh tokens per outcome"
	caption="Fresh tokens, with cached re-reads left out, divided by accepted outcomes; source: usage records and accepted outcomes; lower is better. n is accepted outcomes per week."
	valueFormat={group}
	tickFormat={(tick) => group(tick)}
/>
</div>

<style>
	.kept-panels { display: flex; flex-direction: column; gap: 20px; min-width: 0; }
</style>
