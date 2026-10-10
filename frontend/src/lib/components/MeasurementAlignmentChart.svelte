<script lang="ts">
	import type { MeasurementRow } from '$lib/stores/measurement';
	import type { BarPoint, ChartGeometry } from './measurementBarChart';
	import { ALIGNMENT_PARTS, TOO_FEW, alignmentDesktop, alignmentPeriods, alignmentPhone, sharePercent } from './measurementAlignmentChart';

	let { rows, points, axisTitle, unit = 'week', caption, ariaLabel }: {
		rows: MeasurementRow[];
		/** The periods on the chart's axis, oldest first; one the measure had not started yet holds its place and draws nothing. */
		points: BarPoint[];
		axisTitle: string;
		/** What one bar is, for the table. */
		unit?: string;
		caption: string;
		ariaLabel: string;
	} = $props();

	const periods = $derived(alignmentPeriods(rows, points));
	const phone = $derived(alignmentPhone(periods, axisTitle));
	// The desktop plot is drawn at the card's own width, so text keeps its size and the bars fill the card.
	let dskWidth = $state(0);
	function watchWidth(node: HTMLElement) {
		if (typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(() => { dskWidth = node.clientWidth; });
		observer.observe(node);
		return { destroy: () => observer.disconnect() };
	}
	const desktop = $derived(alignmentDesktop(periods, axisTitle, dskWidth > 0 ? Math.max(560, Math.round(dskWidth)) : undefined));
	const anyWithheld = $derived(periods.some((p) => p.issue === TOO_FEW && p.n > 0 && !p.partial));
	const anyPartial = $derived(periods.some((p) => p.partial));
</script>

{#snippet svg(g: ChartGeometry)}
	<svg viewBox={`0 0 ${g.width} ${Math.trunc(g.height)}`} role="img" aria-label={ariaLabel}>
		{#each g.items as item}
			{#if item.kind === 'text'}
				<text class={item.cls} x={item.x} y={item.y} text-anchor={item.anchor} transform={item.rotate ? `rotate(${item.rotate.deg ?? -90} ${item.rotate.x} ${item.rotate.y})` : undefined}>{item.text}</text>
			{:else if item.kind === 'line'}
				<line class={item.cls} x1={item.x1} x2={item.x2} y1={item.y} y2={item.y} />
			{:else if item.kind === 'rect'}
				<rect class={item.cls} x={item.x} y={item.y} width={item.width} height={item.height} />
			{/if}
		{/each}
	</svg>
{/snippet}

<figure class="chart">
	<div class="lg">
		{#each ALIGNMENT_PARTS as part, index}<span><i class={`s${index}`}></i>{part.label}</span>{/each}
		{#if anyPartial}<span><i class="p"></i>Today so far: the day is still running</span>{/if}
		{#if anyWithheld}<span>No bar, "too few": too few decisions answered that {unit} to show the shares</span>{/if}
	</div>
	<div class="mob">{@render svg(phone)}</div>
	<div class="dsk" use:watchWidth>{@render svg(desktop)}</div>
	<table class="sr-only">
		<caption>{ariaLabel}</caption>
		<thead><tr><th scope="col">{unit === 'day' ? 'Day' : 'Week starting'}</th>{#each ALIGNMENT_PARTS as part}<th scope="col">{part.label}</th>{/each}<th scope="col">n</th></tr></thead>
		<tbody>
			{#each periods.filter((p) => !p.empty) as period}
				<tr><th scope="row">{period.start}</th>{#if period.shares}{#each period.shares as share}<td>{sharePercent(share)}</td>{/each}{:else}<td colspan="3">{period.issue}</td>{/if}<td>{period.n}{period.partial ? ', today so far' : ''}</td></tr>
			{/each}
		</tbody>
	</table>
	<figcaption>{caption}</figcaption>
</figure>

<style>
	/* The --mm-* tokens are the page's chart colours for light and dark; the three shares keep the original chart's colours. */
	.chart {
		--mac-ink: var(--mm-ink, #1b2430);
		--mac-muted: var(--mm-muted, #5b6675);
		--mac-grid: var(--mm-grid, #e3e8ee);
		--mac-halo: var(--mm-card, #fff);
		margin: 0; min-width: 0;
	}
	.chart figcaption { font-size: 12px; color: var(--mac-muted); margin-top: 6px; overflow-wrap: anywhere; }
	.chart svg { display: block; width: 100%; height: auto; }
	.dsk { display: none; }
	.lg { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11.5px; color: var(--mac-muted); margin: 0 0 4px; }
	.lg i { display: inline-block; vertical-align: -1px; margin-right: 5px; width: 10px; height: 10px; }
	.lg .s0, svg .s0 { background: var(--green-text); fill: var(--green-text); }
	.lg .s1, svg .s1 { background: var(--sky); fill: var(--sky); }
	.lg .s2, svg .s2 { background: var(--text-tertiary); fill: var(--text-tertiary); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
	svg text { fill: var(--mac-muted); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
	svg .tk, svg .xl, svg .xm, svg .nn, svg .lo { font-size: 11px; }
	svg .at { font-size: 11px; fill: var(--mac-ink); font-weight: 650; }
	svg .grid { stroke: var(--mac-grid); stroke-width: 1; }
	svg .zero { stroke: var(--mac-ink); stroke-width: 1; }
	svg .stack { stroke: var(--mac-halo); stroke-width: 1; }
	/* The running day is outlined, as the page's bar charts outline theirs. */
	svg .stack.part { fill-opacity: 0.35; stroke: var(--mac-ink); stroke-dasharray: 3 2; }
	.lg .p { border: 1.2px dashed var(--mac-ink); box-sizing: border-box; }
	svg text.sl { fill: var(--mac-halo); font-size: 11.5px; font-weight: 650; }
	@media (min-width: 900px) {
		.mob { display: none; }
		.dsk { display: block; }
		svg .at { font-size: 10.5px; }
		svg .tk, svg .xl, svg text.sl { font-size: 10.5px; }
		svg .xm, svg .nn { font-size: 9.5px; }
		svg .lo { font-size: 8.5px; }
	}
</style>
