<script lang="ts">
	import type { PanelState } from '$lib/stores/measurement';
	import MeasurementAgentsMinutes from './MeasurementAgentsMinutes.svelte';
	import { measurementAgents } from '$lib/stores/measurementAgents';
	import MeasurementPeriodSection from './MeasurementPeriodSection.svelte';
	import { changeOnly, periodView } from './measurementPeriodView';

	let { attention, asks, firstTry, onRetry }: {
		attention: PanelState;
		asks: PanelState;
		firstTry: PanelState;
		onRetry: (panel: string) => void;
	} = $props();

	const asksView = $derived(periodView(asks.rows, 'asks_per_outcome', 1, asks.period, asks.windows?.asks_per_outcome));
	/** One line under the agents table, for all agents together; the kept Asks per outcome chart draws the series. */
	const asksLine = $derived.by(() => {
		const lead = 'Asks are not recorded per agent yet. All agents together:';
		if (asks.loading && !asks.rows.length) return `${lead} loading.`;
		if (asks.error && !asks.rows.length) return `${lead} not available right now.`;
		const value = asksView.current.value;
		return `${lead} ${value === null ? '–' : value.toFixed(2)} asks per outcome, ${changeOnly(asksView)}.`;
	});
</script>

<MeasurementPeriodSection
	id="attention"
	title="Your minutes per outcome"
	state={attention}
	onRetry={() => onRetry('attention')}
	series="active_hours_per_outcome"
	scale={60}
	unitText="minutes per outcome"
	headline={(value) => value.toFixed(1)}
	valueFormat={(value) => value.toFixed(1)}
	chartTitle="Minutes of your engagement per outcome"
	axisTitle="Minutes of your engagement per outcome"
	caption="Your engaged minutes divided by accepted outcomes; source: your time active in CORSIA and accepted outcomes; lower is better. n is accepted outcomes per week."
>
	{#snippet after()}<MeasurementAgentsMinutes state={$measurementAgents} /><p class="asks">{asksLine}</p>{/snippet}
</MeasurementPeriodSection>
<MeasurementPeriodSection
	id="first-try"
	title="First-try rate (agent-graded)"
	state={firstTry}
	onRetry={() => onRetry('first-try')}
	series="first_try_acceptance"
	scale={100}
	unitText="first try"
	headline={(value) => `${value.toFixed(0)}%`}
	chartTitle="Accepted outcomes right on the first try"
	axisTitle="Accepted outcomes right on the first try"
	caption="Share of accepted outcomes right on the first try, graded by the agents; source: agent grades; higher is better. n is accepted outcomes per week."
	percent
/>

<style>
	.asks { margin: 8px 0 0; font-size: 13px; font-style: italic; color: var(--text-secondary); overflow-wrap: anywhere; }
</style>
