<script lang="ts">
	import { agentLabel } from '$lib/stores/measurementAgentLabel';
	import {
		loadMeasurementAgents,
		type AgentsMetric,
		type AgentsState,
	} from '$lib/stores/measurementAgents';
	import { filterMeasurementAgent, formatMeasurementValue } from '$lib/stores/measurement';
	import { formatChange } from '$lib/stores/measurementWhereToLook';
	import { periodName } from '$lib/stores/measurementPeriod';

	let { state: agents, embedded = false }: { state: AgentsState; embedded?: boolean } = $props();

	type Row = AgentsState['rows'][number];

	/** The most rows the table shows: the ten agents with the most outcomes in the latest window. */
	const LIMIT = 10;

	/** The windows the heading and the change column name; without an echoed period the table names none. */
	const latestName = $derived(agents.period ? periodName(agents.period.latest) : null);
	const previousName = $derived(agents.period ? periodName(agents.period.previous) : null);

	/** The column the reader sorted by. The table opens with the most outcomes first. */
	type SortKey = 'agent' | 'outcomes' | AgentsMetric;
	let sortKey = $state<SortKey | null>(null);
	let descending = $state(true);

	/** Click cycles ascending, descending, then back to the opening order. */
	function sortBy(key: SortKey): void {
		if (sortKey !== key) { sortKey = key; descending = false; return; }
		if (!descending) { descending = true; return; }
		sortKey = null; descending = false;
	}

	/**
	 * The change in minutes per outcome against the previous window, as a fraction, or the
	 * reason there is none: 'low n' when the latest window is too thin for a value, 'new'
	 * when the previous one is, so a missing figure is never read as no change.
	 */
	function minutesChange(row: Row): number | 'low n' | 'new' {
		const cell = row.metrics.active_hours_per_outcome;
		if (!cell || cell.value === null) return 'low n';
		const before = cell.previous_value;
		if (before === null || before === undefined || before === 0) return 'new';
		return (cell.value - before) / before;
	}

	function sortValue(row: Row, key: SortKey): number | string | null {
		if (key === 'agent') return row.agent.name.toLowerCase();
		if (key === 'outcomes') return row.n;
		if (key === 'active_hours_per_outcome') {
			const change = minutesChange(row);
			return typeof change === 'number' ? change : null;
		}
		return row.metrics[key]?.value ?? null;
	}

	/**
	 * The ten agents with the most outcomes in the latest window, in the reader's order. The server ranks by
	 * a quality metric, so the row set and the opening order are decided here; a row with no value for the
	 * column goes last in either direction.
	 */
	const shown = $derived.by(() => {
		const top = agents.rows
			.filter((row) => row.n > 0)
			.sort((a, b) => b.n - a.n || a.agent.name.localeCompare(b.agent.name))
			.slice(0, LIMIT);
		const key = sortKey;
		if (key === null) return top;
		const direction = descending ? -1 : 1;
		return [...top].sort((a, b) => {
			const left = sortValue(a, key), right = sortValue(b, key);
			if (left === null && right === null) return 0;
			if (left === null) return 1;
			if (right === null) return -1;
			return left < right ? -direction : left > right ? direction : 0;
		});
	});

	function ariaSort(key: SortKey): 'ascending' | 'descending' | 'none' {
		return sortKey === key ? (descending ? 'descending' : 'ascending') : 'none';
	}

	function dollars(value: number): string {
		return `$${value.toFixed(2)}`;
	}

	/** One reading of a cell, used for the visible text, its hover label and its screen-reader text alike. */
	function cellText(value: number | null | undefined, format: (value: number) => string): string | null {
		return value === null || value === undefined ? null : format(value);
	}
</script>

<section class="agents" class:embedded aria-labelledby="agents-title">
	<h3 id="agents-title">{latestName ? `Agents, ${latestName}` : 'Agents'}</h3>

	{#if agents.loading && !agents.rows.length}
		<div class="skeleton" aria-label="Loading the agents table"></div>
	{:else if agents.error && !agents.rows.length}
		<p class="error" role="alert">{agents.error}<button onclick={() => void loadMeasurementAgents()}>Retry</button></p>
	{:else if !agents.rows.length}
		<p class="empty">No active agents in this scope.</p>
	{:else}
		<!-- A scrollable table is only reachable by keyboard if the scroll box itself
		     takes focus, which is what the platform expects of a panable region; the
		     a11y rule below is written for decorative tabindex, not for this. -->
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<div class="table-wrap" role="region" aria-label="Agents table, scrolls sideways when it is wider than the screen" tabindex="0">
		<table>
			<caption class="sr-only">
				The ten agents with the most outcomes in the latest window, most first until a column header is chosen. A value is
				hidden until the agent has enough outcomes for it, and an unmeasured figure is never shown as zero.
			</caption>
			<thead>
				<tr>
					<th scope="col" aria-sort={ariaSort('agent')}><button class="sort" onclick={() => sortBy('agent')}>Agent</button></th>
					<th scope="col" class="n" aria-sort={ariaSort('outcomes')}><button class="sort" onclick={() => sortBy('outcomes')}>Outcomes</button></th>
					<th scope="col" class="n" aria-sort={ariaSort('first_try_acceptance')}><button class="sort" onclick={() => sortBy('first_try_acceptance')}>First try</button></th>
					<th scope="col" class="n cost" aria-sort={ariaSort('dollars_per_work_hour')}><button class="sort" onclick={() => sortBy('dollars_per_work_hour')}>$ per hour of work</button></th>
					<th scope="col" class="n change" aria-sort={ariaSort('active_hours_per_outcome')}><button class="sort" onclick={() => sortBy('active_hours_per_outcome')}>Change in minutes per outcome, vs {previousName ?? 'the previous window'}</button></th>
				</tr>
			</thead>
			<tbody>
				{#each shown as row (row.agent.id)}
					{@const thin = row.status === 'too_few'}
					{@const first = row.metrics.first_try_acceptance}
					{@const cost = row.metrics.dollars_per_work_hour}
					{@const change = minutesChange(row)}
					{@const firstText = cellText(first?.value, (value) => formatMeasurementValue(value, 'percent'))}
					{@const costText = cellText(cost?.value, dollars)}
					<!-- The whole row is the filter target; the name inside it is the keyboard and screen-reader way in. -->
					<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
					<tr class:thin onclick={() => filterMeasurementAgent(row.agent.id, row.agent.project.id)}>
						<th scope="row">
							<button class="filter name" aria-label={`Filter to this agent: ${agentLabel(row.agent.name)}`}>{agentLabel(row.agent.name)}</button>
							{#if thin}<span class="sr-only">Too few outcomes to rank</span>{/if}
						</th>
						<td class="n">{formatMeasurementValue(row.n, 'count')}</td>
						<td class="n" title={firstText ?? 'Unmeasured'}>
							{#if firstText === null}<span class="unmeasured">&ndash;</span><span class="sr-only">Unmeasured</span>{:else}{firstText}{/if}
						</td>
						<td class="n" title={costText ?? 'Unmeasured'}>
							{#if costText === null}<span class="unmeasured">&ndash;</span><span class="sr-only">Unmeasured</span>{:else}{costText}{/if}
						</td>
						<td class="n" title={typeof change === 'number' ? formatChange(change) : change}>
							{#if typeof change === 'number'}{formatChange(change)}<span class="sr-only"> against {previousName ?? 'the previous window'}</span>{:else if change === 'new'}new{:else}<span class="unmeasured">{change}</span>{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
		</div>
		{#if agents.error}<p class="error" role="alert">{agents.error}<button onclick={() => void loadMeasurementAgents()}>Retry</button></p>
		{:else if agents.loading}<p class="refreshing" aria-live="polite">Refreshing…</p>{/if}
	{/if}
</section>

<style>
	.agents { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: 0; padding: 16px; display: grid; gap: 6px; }
	h3, p { margin: 0; }
	h3 { margin-top: 20px; color: var(--text-secondary); font-size: var(--text-footnote); letter-spacing: 0.05em; text-transform: uppercase; }
	p { color: var(--text-secondary); font-size: var(--text-subhead); }
	.agents.embedded { background: none; border: 0; border-radius: 0; padding: 0; }
	.sort { border: 0; background: none; color: inherit; font: inherit; font-weight: inherit; padding: 0; cursor: pointer; text-decoration: underline dotted; text-align: inherit; }
	[aria-sort='ascending'] .sort::after { content: ' \2191'; } [aria-sort='descending'] .sort::after { content: ' \2193'; }
	.table-wrap { overflow-x: auto; }
	table { width: 100%; min-width: 320px; border-collapse: collapse; color: var(--text-primary); font-size: var(--text-subhead); }
	th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--border-subtle); vertical-align: baseline; }
	thead th { font-size: var(--text-footnote); color: var(--text-secondary); font-weight: 600; vertical-align: bottom; }
	tbody th, tbody td { white-space: nowrap; font-weight: 400; }
	tr.thin td, tr.thin .filter { color: var(--text-secondary); }
	.n { text-align: right; font-variant-numeric: tabular-nums; }
	.unmeasured { color: var(--text-secondary); font-style: italic; }
	tbody tr { cursor: pointer; } tbody tr:hover { background: var(--bg-hover); }
	.filter { border: 0; background: none; color: inherit; font: inherit; padding: 0; cursor: pointer; text-align: left; }
	.filter:hover { text-decoration: underline; }
	.filter:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.error { color: var(--red-text); display: flex; align-items: center; gap: 8px; }
	.error button { border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); background: var(--bg-primary); color: var(--text-primary); padding: 4px 8px; cursor: pointer; }
	.refreshing { color: var(--text-secondary); font-size: var(--text-footnote); }
	.empty { min-height: 48px; display: flex; align-items: center; }
	.skeleton { height: 120px; border-radius: var(--radius-xs); background: var(--bg-hover); animation: pulse 1.2s ease-in-out infinite alternate; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
	.table-wrap:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* Phone: every column stays in view. Names and headers wrap between words (never inside one), the figures stay on one line, and the long change header gets a narrow column of its own. */
	@media (max-width: 430px) {
		table { width: 100%; min-width: 0; table-layout: fixed; font-size: var(--text-footnote); }
		th, td { padding: 6px 3px; }
		tbody th { white-space: normal; }
		.filter { white-space: normal; overflow-wrap: normal; word-break: normal; }
		thead th { white-space: normal; overflow-wrap: normal; word-break: normal; font-size: var(--text-caption); }
		.sort { white-space: normal; }
		/* Fixed table layout otherwise divides the unconstrained first three columns
		   evenly, leaving too little room for an unbroken agent-name word. */
		thead th:first-child { width: 28%; }
		thead th:nth-child(2), thead th:nth-child(3) { width: 10%; }
		thead th.cost { width: 17%; }
		thead th.change { width: 35%; }
	}
	/* The narrowest phones: tighter padding keeps the longest name ("Infrastructure") from pushing the last column out. */
	@media (max-width: 380px) {
		th, td { padding: 6px 2px; }
	}
	@keyframes pulse { to { opacity: .55; } }
</style>
