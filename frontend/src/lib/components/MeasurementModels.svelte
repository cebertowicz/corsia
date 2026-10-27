<script lang="ts">
	import {
		loadMeasurementModels, MODEL_SIZE_CLASSES, type ModelRateReason, type ModelSizeCell, type ModelsState,
	} from '$lib/stores/measurementModels';
	import { periodName } from '$lib/stores/measurementPeriod';
	import { humanTokens } from '$lib/format';

	type Row = ModelsState['rows'][number];
	type Share = ModelsState['tokenShares'][number];

	/** The first size class a row has a cell for: on a phone it carries the "$ per task, by size class" label. */
	function leadSize(row: Row): string | undefined {
		return sizeColumns.find((size) => cell(row, size));
	}
	/** Show the blended $ per accepted task beside the size columns; off, it shows only for a row with no size cell. */
	const BLENDED_COLUMN = false;

	let { state }: { state: ModelsState } = $props();
	/** The Other row is dropped when no model fell under the floor. */
	const rows = $derived(state.rows.filter((row) => !(row.other && row.tasks === 0)));
	const sizeColumns = $derived(MODEL_SIZE_CLASSES.filter((size) => rows.some((row) => row.sizes.some((cell) => cell.size === size))));
	const blendedColumn = $derived(BLENDED_COLUMN || !sizeColumns.length);
	const cells = $derived(sizeColumns.length ? rows.flatMap((row) => row.sizes) : []);
	/** Rows whose blended figure is on screen, so the footnotes name only marks that are shown. */
	const blendedRows = $derived(blendedColumn ? rows : rows.filter((row) => !row.sizes.length));
	const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
	/** Blue first, as the approved render draws it; the same in both themes. */
	const colors = ['#2f6fdb', '#0f8a6c', '#d9822b', '#8a5cc2', '#c4476a', '#6b7787'];
	/** The "Other (N more models)" slice's own neutral, lighter than the slate slice in both themes. */
	const restColor = '#9aa4b2';
	/** The window's whole API-equivalent spend, served: ledger dollars of every call, with or without a row here. */
	const totalSpend = $derived(state.totalSpendUsd);
	const windowSuffix = $derived(state.period ? `, ${periodName(state.period.latest)}` : '');
	const pieHeading = $derived(`Share of tokens by model${windowSuffix}`);

	function percent(value: number): string {
		return `${(value * 100).toFixed(1)}%`;
	}
	/** The row's share of the window's API-equivalent spend; a dash where nothing was priced. */
	function spendShare(row: Row): string {
		return row.spend_usd === null || totalSpend <= 0 ? '—' : percent(row.spend_usd / totalSpend);
	}
	/** A known rate that leaves out unpriced calls, as Cost shows its known amount. */
	function partial(row: Row): boolean {
		return row.recorded_cost_per_task !== null && row.cost_class === 'no_pricing';
	}
	/** The one label for each reason the backend serves; '—' where there is no figure and no cause to name. */
	const RATE_LABELS: Record<Exclude<ModelRateReason, 'none'>, string> = { no_tasks: '—', unpriced: 'unpriced', local: 'no list price' };
	function rate(row: Row): string {
		if (row.rate_reason !== 'none') return RATE_LABELS[row.rate_reason];
		if (row.recorded_cost_per_task === null) return '—';
		// A call that never reached the ledger leaves the figure a lower bound, as Cost words it.
		const amount = `${row.recording_gap ? 'at least ' : ''}${usd.format(row.recorded_cost_per_task)}`;
		return `${amount}${partial(row) ? '*' : ''}`;
	}
	function cell(row: Row, size: string): ModelSizeCell | undefined {
		return row.sizes.find((entry) => entry.size === size);
	}
	/** One class's figure and its change: "$1.07 -7%", or "new" when the previous week had too few of the class. */
	function sizeRate(entry: ModelSizeCell): string {
		const amount = `${entry.recording_gap ? 'at least ' : ''}${usd.format(entry.cost_per_task)}${entry.partial ? '*' : ''}`;
		if (entry.change_reason === 'new') return `${amount} new`;
		if (entry.cost_change === null) return amount;
		return `${amount} ${entry.cost_change >= 0 ? '+' : ''}${Math.round(entry.cost_change * 100)}%`;
	}
	function firstTry(entry: ModelSizeCell): string {
		return `${Math.round(entry.first_try * 100)}% first try`;
	}
	/** The backend names at most six slices, one palette color each, then one "Other (N more models)" slice. */
	function color(share: Share, index: number): string {
		return share.model === '' ? restColor : colors[index] ?? restColor;
	}
	function gradient(): string {
		let start = 0;
		const stops = state.tokenShares.map((share, index) => {
			const end = start + share.share_of_tokens * 100;
			const stop = `${color(share, index)} ${start}% ${end}%`;
			start = end;
			return stop;
		});
		return `conic-gradient(${stops.join(', ')})`;
	}
</script>

<section class="models" aria-labelledby="models-title">
	<h3 id="models-title">Models{windowSuffix}</h3>
	{#if state.loading && !state.rows.length}
		<div class="skeleton" aria-label="Loading models"></div>
	{:else if state.error && !state.rows.length}
		<div class="error" role="alert">
			{state.error}<button onclick={() => void loadMeasurementModels()}>Retry</button>
		</div>
	{:else if !rows.length}
		<p class="empty">No models were used for this selection.</p>
	{:else}
		<div class="body">
			<div class="table-wrap">
				<table>
					<thead>
						{#if sizeColumns.length}
							<tr>
								<th class="model" scope="col" rowspan="2">Model</th>
								<th class="spend-h" scope="col" rowspan="2">Share of spend</th>
								<th class="by-size" scope="colgroup" colspan={sizeColumns.length}>$ per task, by size class</th>
								<th class="tasks-h" scope="col" rowspan="2">Tasks</th>
							</tr>
							<tr class="sub">
								{#each sizeColumns as size (size)}<th scope="col">{size}</th>{/each}
							</tr>
						{:else}
							<tr>
								<th class="model" scope="col">Model</th>
								<th class="spend-h" scope="col">Share of spend</th>
								<th scope="col">$ per accepted task</th>
								<th class="tasks-h" scope="col">Tasks</th>
							</tr>
						{/if}
					</thead>
					<tbody>
						{#each rows as row (row.model)}
							<tr>
								<th scope="row">
									<span class="name">{row.label}</span>
									{#if row.not_offered_now}<span class="not-offered">not offered now</span>{/if}
									{#if row.includes_local}<span class="qualifier">includes local models with no list price</span>{/if}
								</th>
								<td class="spend" class:nospend={row.spend_usd === null || totalSpend <= 0}>{spendShare(row)}</td>
								{#if blendedColumn}<td class="blended">{rate(row)}</td>{/if}
								{#if sizeColumns.length}
									{#if !row.sizes.length && !BLENDED_COLUMN}
										<td class="blended span" colspan={sizeColumns.length}>
											{rate(row)}{#if row.rate_reason === 'none' && row.recorded_cost_per_task !== null}{' per accepted task, all sizes'}{/if}
										</td>
									{:else}
										{#each sizeColumns as size (size)}
											{@const entry = cell(row, size)}
											{#if entry}
												<td class="size" class:lead={size === leadSize(row)}><span class="size-label">{size}</span> <span>{sizeRate(entry)}</span> <span class="first-try"><span class="sep">{'· '}</span>{firstTry(entry)}</span></td>
											{:else}
												<td class="size none">—</td>
											{/if}
										{/each}
									{/if}
								{/if}
								<td class="tasks">{row.tasks}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
			<div class="share" aria-label="Token share by model">
				<h4>{pieHeading}</h4>
				<div class="pie-row">
					<div
						class="pie"
						style:background={gradient()}
						role="img"
						aria-label={state.tokenShares.map((share) => `${share.label} ${percent(share.share_of_tokens)}, ${humanTokens(share.tokens)} tokens`).join(', ')}
					></div>
					<ul class="legend">
						{#each state.tokenShares as share, index (share.model)}
							<li><i style:background={color(share, index)}></i><span>{share.label}</span> <b>{percent(share.share_of_tokens)}</b> <em>{humanTokens(share.tokens)}</em></li>
						{/each}
					</ul>
				</div>
			</div>
		</div>
		<p class="note">
			Models{windowSuffix}.
			{#if sizeColumns.length}
				$ per task within each size class, whole chain, API-equivalent; a class shows only with {state.minTasks ?? 14} or more tasks.
				A change compares one named size class that has {state.minTasks ?? 14} or more tasks in both windows, otherwise n/a, mix changed.
			{:else}
				$ per accepted task, whole chain, API-equivalent.
			{/if}
		</p>
		{#if blendedRows.some(partial) || cells.some((entry) => entry.partial)}
			<p class="note">* Some calls were unpriced; the figure covers priced calls only.</p>
		{/if}
		{#if blendedRows.some((row) => row.recording_gap) || cells.some((entry) => entry.recording_gap)}
			<p class="note">"At least": a call failed to record, so the figure may be low.</p>
		{/if}
		{#if state.unresolvedTokenShare > 0}
			<p class="note">{percent(state.unresolvedTokenShare)} of tokens ran under a model alias no record resolves; it is shown under the alias name.</p>
		{/if}
		{#if state.error}
			<p class="error" role="alert">{state.error}<button onclick={() => void loadMeasurementModels()}>Retry</button></p>
		{/if}
		{#if state.loading}<p class="refreshing" aria-live="polite">Refreshing…</p>{/if}
	{/if}
</section>

<style>
	/* No card of its own: the Cost card around it supplies the surface. */
	.models {
		display: grid;
		gap: 10px;
		min-width: 0;
	}
	h3 { margin: 0; color: var(--text-secondary); font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; overflow-wrap: anywhere; }
	p { color: var(--text-secondary); font-size: var(--text-subhead); }
	.body { display: grid; gap: 18px; min-width: 0; }
	.table-wrap { min-width: 0; }
	/* The table fills the card; the model, share and tasks columns keep fixed shares and the size columns split the rest. */
	table {
		width: 100%;
		border-collapse: collapse;
		color: var(--text-primary);
		font-size: 13.5px;
	}
	th,
	td {
		overflow-wrap: break-word;
		text-align: left;
		padding: 10px 10px 10px 0;
		border-top: 1px solid var(--border-subtle);
		vertical-align: middle;
	}
	thead th { color: var(--text-secondary); font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; border-top: 0; padding-top: 0; padding-bottom: 6px; vertical-align: bottom; }
	thead tr.sub th { padding-top: 0; }
	thead th.by-size { padding-bottom: 2px; }
	thead th.model,
	tbody th { width: 22%; }
	thead th.spend-h,
	td.spend { width: 11%; }
	thead th.tasks-h,
	td.tasks { width: 8%; }
	tbody th { font-weight: 700; }
	.size span { white-space: nowrap; }
	/* Wide: the rate, then its first-try share on its own line, so the table fits beside the pie. */
	.first-try { display: block; color: var(--text-secondary); }
	.first-try .sep { display: none; }
	.size-label { display: none; }
	/* Neutral facts about the row, never an error: muted, not red. */
	.not-offered,
	.qualifier { display: block; font-size: var(--text-footnote); font-weight: 400; color: var(--text-secondary); }
	.share { min-width: 0; align-self: start; }
	/* The approved chart-heading style, as the other Measurement charts. */
	.share h4 { margin: 0 0 8px; color: var(--text-secondary); font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; overflow-wrap: anywhere; }
	.pie-row { display: grid; gap: 14px; align-items: center; min-width: 0; }
	.pie {
		width: min(300px, 100%);
		aspect-ratio: 1;
		border-radius: 50%;
		margin: 0 auto;
	}
	.legend {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		font-size: 13px;
		color: var(--text-primary);
	}
	.legend li { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto auto; align-items: center; column-gap: 8px; padding: 4px 0; border-top: 1px solid var(--border-subtle); }
	.legend span { overflow-wrap: anywhere; }
	.legend i { width: 12px; height: 12px; }
	.legend b { font-weight: 650; font-variant-numeric: tabular-nums; }
	.legend em { font-style: normal; color: var(--text-secondary); min-width: 46px; text-align: right; font-variant-numeric: tabular-nums; }
	.skeleton {
		height: 96px;
		border-radius: var(--radius-xs);
		background: var(--bg-hover);
		animation: pulse 1.2s ease-in-out infinite alternate;
	}
	.empty,
	.error { color: var(--text-secondary); }
	.error { color: var(--red-text); display: flex; gap: 8px; }
	.error button {
		border: 1px solid var(--border-subtle);
		background: var(--bg-primary);
		color: var(--text-primary);
		border-radius: var(--radius-xs);
	}
	.note,
	.refreshing { font-size: var(--text-footnote); }
	@keyframes pulse { to { opacity: 0.55; } }
	@media (min-width: 760px) {
		.pie-row { grid-template-columns: 320px minmax(0, 1fr); }
		.pie { margin: 0; }
	}
	/* Narrow: each model is a block, its name and task count on one line, then one short line per size class. */
	@media (max-width: 600px) {
		table,
		tbody { display: block; }
		thead {
			position: absolute;
			width: 1px;
			height: 1px;
			overflow: hidden;
			clip-path: inset(50%);
		}
		/* A model is a block: its name, the labelled Share and Tasks pair, then its size cells. */
		tbody tr { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px; padding: 10px 0; border-top: 1px solid var(--border-subtle); }
		tbody th,
		tbody td { display: block; border: 0; width: auto; padding: 0; min-width: 0; }
		tbody th { grid-column: 1 / -1; }
		td.spend,
		td.tasks { width: auto; font-weight: 650; }
		td.spend { order: 1; }
		td.tasks { order: 2; }
		td.spend::before,
		td.tasks::before { display: block; font-size: 11px; font-weight: 400; color: var(--text-secondary); }
		td.spend::before { content: 'Share of spend'; }
		td.tasks::before { content: 'Tasks'; }
		td.nospend,
		td.none { display: none; }
		td.size,
		td.blended { order: 3; grid-column: 1 / -1; }
		td.size.lead::before { content: '$ per task, by size class'; display: block; font-size: 11px; color: var(--text-secondary); }
		.size-label { display: inline; font-weight: 600; }
		.size span { white-space: normal; }
		.first-try,
		.first-try .sep { display: inline; }
	}
</style>
