<script lang="ts">
	import type { KeyFiguresState } from '$lib/stores/keyFigures';
	import { measurementGrain } from '$lib/stores/measurementGrain';
	import { loadMeasurementCost, measurementCost } from '$lib/stores/measurementCost';
	import { loadMeasurementPanel, measurementPanels } from '$lib/stores/measurement';
	import { KPI_MEASURES, measureTiles, tokensTile, type KpiTile } from './measurementKpiTiles';

	/** The row answers for whichever grain the page is on; each figure is the one its section below headlines. */
	let { state: figures, onRetry }: { state: KeyFiguresState; onRetry: () => void } = $props();

	const order: { id: KpiTile['id']; label: string }[] = [
		{ id: 'work', label: 'Work per hour of your attention' },
		{ id: 'first-try', label: 'First-try rate (agent-graded)' },
		{ id: 'cost', label: 'Cost per person-hour of work delivered' },
		{ id: 'minutes', label: 'Your minutes per outcome' },
		{ id: 'tokens', label: 'Tokens' },
	];
	const panels = $derived($measurementPanels);
	const cost = $derived($measurementCost);
	const measures = $derived(measureTiles(panels, cost.grain === $measurementGrain ? cost.data : null));
	const tiles = $derived<Partial<Record<KpiTile['id'], KpiTile>>>({ ...measures, ...(figures.kpi ? { tokens: tokensTile(figures.kpi) } : {}) });
	/** Whether the source a tile waits on failed, and what retrying it reloads: a windowed measure follows its own section's panel. */
	const sources = $derived<Record<KpiTile['id'], { error: boolean; retry: () => void }>>({
		work: { error: !!panels[KPI_MEASURES.work.panel]?.error, retry: () => void loadMeasurementPanel(KPI_MEASURES.work.panel) },
		'first-try': { error: !!panels[KPI_MEASURES['first-try'].panel]?.error, retry: () => void loadMeasurementPanel(KPI_MEASURES['first-try'].panel) },
		minutes: { error: !!panels[KPI_MEASURES.minutes.panel]?.error, retry: () => void loadMeasurementPanel(KPI_MEASURES.minutes.panel) },
		cost: { error: !!cost.error && !cost.data, retry: () => void loadMeasurementCost() },
		tokens: { error: !!figures.error, retry: onRetry },
	});
</script>

<div class="frame">
<section class="kpis" aria-label="Key figures for the latest window">
	{#each order as slot (slot.id)}
		{@const tile = tiles[slot.id]}
		<div class="tile" data-kpi={slot.id}>
			<span class="label">{slot.label}</span>
			{#if tile}
				<strong>{tile.value}</strong>
				<small>{tile.sub}</small>
				{#if tile.parts}
					<dl>{#each tile.parts as part (part.id)}<div data-token={part.id}><dt>{part.label}</dt><dd>{part.value}</dd></div>{/each}</dl>
				{/if}
			{:else if sources[slot.id].error}
				<div class="error" role="alert">Couldn't load this figure.<button onclick={sources[slot.id].retry}>Retry</button></div>
			{:else}
				<div class="skeleton" aria-label={`Loading ${slot.label}`}></div>
			{/if}
		</div>
	{/each}
</section>
</div>

<style>
	.frame { container-type: inline-size; min-width: 0; }
	/* Tiles in a row share the tallest one's height, as in r2.7, so no grey shows under a short tile. */
	.kpis { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; min-width: 0; }
	/* Five tiles share the row only when the frame is wide. */
	@container (min-width: 720px) { .kpis { grid-template-columns: repeat(5, minmax(0, 1fr)); } }
	/* From 900px the Tokens tile gets a 270px track, enough for its four parts in one row; below that the five tiles are equal and the parts take two by two. */
	@container (min-width: 900px) { .kpis { grid-template-columns: repeat(4, minmax(0, 1fr)) 270px; } }
	.tile { container: kpi-tile / inline-size; display: flex; flex-direction: column; background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: 0; padding: 12px; min-width: 0; }
	/* One type scale on every tile: label 12px/650, value 28px/750, sub-line 12px/400. */
	.label { color: var(--text-secondary); font-size: 12px; font-weight: 650; }
	strong { color: var(--text-primary); font-size: 28px; font-weight: 750; line-height: 1.1; margin: 4px 0 2px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
	small { color: var(--text-secondary); font-size: 12px; font-weight: 400; }
	dl { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; margin: 8px 0 0; padding-top: 6px; border-top: 1px solid var(--border-subtle); }
	/* The Tokens parts sit in one row of four, as in r2.7, when the tile is about 210px or wider (186px inside its padding and border); a narrower tile takes them two by two so no word breaks. It follows the dl rule, which it overrides. */
	@container kpi-tile (max-width: 185.98px) { dl { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
	dl > div { display: flex; flex-direction: column; min-width: 0; }
	dt { color: var(--text-secondary); font-size: 11px; }
	dd { margin: auto 0 0; overflow-wrap: anywhere; color: var(--text-primary); font-size: 14px; font-weight: 650; font-variant-numeric: tabular-nums; }
	.error { display: flex; align-items: center; gap: 8px; margin-top: 8px; color: var(--red-text); font-size: 12px; }
	.error button { font: inherit; color: var(--text-primary); background: var(--bg-primary); border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); padding: 5px 9px; cursor: pointer; }
	.skeleton { min-height: 56px; margin-top: 8px; border-radius: var(--radius-xs); background: var(--bg-hover); }
</style>
