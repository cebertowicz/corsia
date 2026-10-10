<script lang="ts">
	import type { AgentsState } from '$lib/stores/measurementAgents';
	import { formatMeasurementValue } from '$lib/stores/measurement';
	import { periodName } from '$lib/stores/measurementPeriod';

	let { state: agents }: { state: AgentsState } = $props();

	/** The most rows shown: the agents that take the most of your minutes per outcome. */
	const LIMIT = 5;

	/** Agents with a measured value, the costliest first; an unmeasured agent is left out, never ranked as zero. */
	const top = $derived(
		agents.rows
			.flatMap((row) => {
				const hours = row.metrics.active_hours_per_outcome?.value;
				return hours === null || hours === undefined ? [] : [{ row, minutesPerOutcome: hours * 60 }];
			})
			.sort((a, b) => b.minutesPerOutcome - a.minutesPerOutcome)
			.slice(0, LIMIT),
	);
	/** One decimal under 10, whole numbers from 10 up, the same reading everywhere the figure appears. */
	function minutesText(value: number): string {
		return value < 10 ? value.toFixed(1) : Math.round(value).toLocaleString('en-US');
	}
	const latestName = $derived(agents.period ? periodName(agents.period.latest) : null);
</script>

{#if top.length}
	<h4 id="agents-minutes-title">Agents with the most of your minutes per outcome{latestName ? `, ${latestName}` : ''}</h4>
	<table aria-labelledby="agents-minutes-title">
		<thead>
			<tr>
				<th scope="col">Agent</th>
				<th scope="col" class="n">Minutes per outcome</th>
				<th scope="col" class="n">Your minutes</th>
				<th scope="col" class="n">Outcomes</th>
			</tr>
		</thead>
		<tbody>
			{#each top as { row, minutesPerOutcome } (row.agent.id)}
				<tr>
					<th scope="row">{row.agent.name}</th>
					<td class="n">{minutesText(minutesPerOutcome)}</td>
					<td class="n">{#if row.active_minutes === null || row.active_minutes === undefined}<span class="unmeasured">&ndash;</span><span class="sr-only">Unmeasured</span>{:else}{minutesText(row.active_minutes)}{/if}</td>
					<td class="n">{formatMeasurementValue(row.n, 'count')}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{/if}

<style>
	h4 { margin: 20px 0 6px; color: var(--text-secondary); font-size: var(--text-footnote); letter-spacing: 0.05em; text-transform: uppercase; }
	table { width: 100%; border-collapse: collapse; color: var(--text-primary); font-size: var(--text-subhead); }
	th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--border-subtle); vertical-align: baseline; }
	/* A header wraps between whole words only; a long agent name may still break inside itself. */
	thead th { font-size: var(--text-footnote); color: var(--text-secondary); font-weight: 600; vertical-align: bottom; }
	tbody th, tbody td { font-weight: 400; }
	tbody th { overflow-wrap: anywhere; }
	.n { text-align: right; font-variant-numeric: tabular-nums; }
	.unmeasured { color: var(--text-secondary); font-style: italic; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
