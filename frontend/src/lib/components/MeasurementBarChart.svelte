<script lang="ts">
	import { desktopGeometry, hasClippedBar, isLow, isWithheld, phoneGeometry, type BarPoint, type ChartGeometry } from './measurementBarChart';

	let { points, axisTitle, unit = 'week', averageName, caption, minN, minNText, ariaLabel, tickFormat, valueFormat, percent100 = false, showN = true }: {
		points: BarPoint[];
		/** Names the measure and its unit; it is the y-axis title. */
		axisTitle: string;
		/** What one bar is, for the legend. */
		unit?: string;
		/** The line's name, from the served moving-average length. */
		averageName: string;
		/** One line: definition, source, which direction is good and what n counts. */
		caption: string;
		minN: number;
		/** The rule behind a hollow bar, e.g. "fewer than 20 outcomes". */
		minNText: string;
		ariaLabel: string;
		tickFormat: (value: number, decimals: number) => string;
		valueFormat: (value: number) => string;
		percent100?: boolean;
		showN?: boolean;
	} = $props();

	const spec = $derived({ points, axisTitle, tickFormat, valueFormat, minN, percent100, showN });
	const phone = $derived(phoneGeometry(spec));
	// The desktop plot is drawn at the card's own width, so text keeps its size and the bars fill the card.
	let dskWidth = $state(0);
	function watchWidth(node: HTMLElement) {
		if (typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(() => { dskWidth = node.clientWidth; });
		observer.observe(node);
		return { destroy: () => observer.disconnect() };
	}
	const plotWidth = $derived(dskWidth > 0 ? Math.max(560, Math.round(dskWidth)) : undefined);
	const desktop = $derived(desktopGeometry(spec, plotWidth));
	const anyLow = $derived(points.some((p) => isLow(p, minN)));
	const anyClip = $derived(!percent100 && hasClippedBar(points, minN));
	const anyWithheld = $derived(points.some(isWithheld));
	const anyPartial = $derived(points.some((p) => p.partial));
	const hasAverage = $derived(points.some((p) => p.ma !== null));
</script>

{#snippet svg(g: ChartGeometry)}
	<svg viewBox={`0 0 ${g.width} ${Math.trunc(g.height)}`} role="img" aria-label={ariaLabel}>
		{#each g.items as item}
			{#if item.kind === 'text'}
				<text class={item.cls} x={item.x} y={item.y} text-anchor={item.anchor} transform={item.rotate ? `rotate(${item.rotate.deg ?? -90} ${item.rotate.x} ${item.rotate.y})` : undefined} style={item.size ? `font-size: ${item.size}px` : undefined}>{item.text}</text>
			{:else if item.kind === 'line'}
				<line class={item.cls} x1={item.x1} x2={item.x2} y1={item.y} y2={item.y} />
			{:else if item.kind === 'rect'}
				<rect class={item.cls} x={item.x} y={item.y} width={item.width} height={item.height} />
			{:else if item.kind === 'path'}
				<path class={item.cls} d={item.d} />
			{:else if item.kind === 'polyline'}
				<polyline class={item.cls} points={item.points} />
			{:else}
				<circle class={item.cls} cx={item.cx} cy={item.cy} r={item.r} />
			{/if}
		{/each}
	</svg>
{/snippet}

<figure class="chart">
	<div class="lg">
		<span><i class="b"></i>One {unit}</span><span><i class="c"></i>Latest {unit}</span>
		{#if anyPartial}<span><i class="p"></i>Today so far: the day is still running</span>{/if}
		{#if anyLow}<span><i class="h"></i>Hollow bar: {minNText} (low n){anyClip ? '; a bar taller than the axis is cut at the top and keeps its value' : ''}</span>{/if}
		{#if anyWithheld}<span>No bar, "too few": too little behind that {unit} to show a value</span>{/if}
		{#if hasAverage}<span><i class="l"></i>{averageName}</span>{/if}
	</div>
	<div class="mob">{@render svg(phone)}</div>
	<div class="dsk" use:watchWidth>{@render svg(desktop)}</div>
	<table class="sr-only">
		<caption>{ariaLabel}</caption>
		<thead><tr><th scope="col">{unit === 'day' ? 'Day' : 'Week starting'}</th><th scope="col">{axisTitle}</th><th scope="col">n</th>{#if hasAverage}<th scope="col">{averageName}</th>{/if}</tr></thead>
		<tbody>
			{#each points.filter((p) => !p.empty) as point}
				<tr><th scope="row">{point.start}</th><td>{point.value === null ? 'no value' : valueFormat(point.value)}{point.partial ? ', today so far' : isLow(point, minN) ? `, ${minNText}` : ''}</td><td>{point.n}</td>{#if hasAverage}<td>{point.ma === null ? 'none' : valueFormat(point.ma)}</td>{/if}</tr>
			{/each}
		</tbody>
	</table>
	<figcaption>{caption}</figcaption>
</figure>

<style>
	/* The --mm-* tokens are the render's colours, set by MeasurementPage for light and dark; the fallbacks are the light values. */
	.chart {
		--mbc-ink: var(--mm-ink, #1b2430);
		--mbc-muted: var(--mm-muted, #5b6675);
		--mbc-bar: var(--mm-bar, #9aa4b2);
		--mbc-current: var(--mm-current, #2f6fdb);
		--mbc-grid: var(--mm-grid, #e3e8ee);
		--mbc-halo: var(--mm-card, #fff);
		margin: 0; min-width: 0;
	}
	.chart figcaption { font-size: 12px; color: var(--mbc-muted); margin-top: 6px; overflow-wrap: anywhere; }
	.chart svg { display: block; width: 100%; height: auto; }
	.dsk { display: none; }
	.lg { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11.5px; color: var(--mbc-muted); margin: 0 0 4px; }
	.lg i { display: inline-block; vertical-align: -1px; margin-right: 5px; }
	.lg .b { width: 10px; height: 10px; background: var(--mbc-bar); }
	.lg .c { width: 10px; height: 10px; background: var(--mbc-current); }
	.lg .p { width: 10px; height: 10px; border: 1.2px dashed var(--mbc-current); }
	.lg .h { width: 10px; height: 10px; border: 1.2px solid var(--mbc-bar); }
	.lg .l { width: 16px; height: 0; border-top: 2px solid var(--mbc-ink); vertical-align: 3px; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
	svg text { fill: var(--mbc-muted); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
	svg .tk, svg .xl, svg .xm, svg .nn, svg .lo { font-size: 11px; }
	svg .vl { font-size: 11.5px; fill: var(--mbc-ink); font-weight: 650; paint-order: stroke; stroke: var(--mbc-halo); stroke-width: 4.5px; stroke-linejoin: round; }
	svg .at { font-size: 11px; fill: var(--mbc-ink); font-weight: 650; }
	svg .grid { stroke: var(--mbc-grid); stroke-width: 1; }
	svg .zero { stroke: var(--mbc-ink); stroke-width: 1; }
	svg .bar { fill: var(--mbc-bar); }
	svg .bar.cur { fill: var(--mbc-current); }
	svg .bar.low { fill: none; stroke: var(--mbc-bar); stroke-width: 1.2; }
	svg .bar.part { fill: none; stroke: var(--mbc-current); stroke-width: 1.4; stroke-dasharray: 3 2; }
	svg .bar.low.cur { stroke: var(--mbc-current); }
	svg .ma { fill: none; stroke: var(--mbc-ink); stroke-width: 1.6; }
	svg .xl.cur { font-weight: 750; fill: var(--mbc-ink); }
	svg .brk { fill: none; stroke: var(--mbc-halo); stroke-width: 1.6; }
	@media (min-width: 900px) {
		.mob { display: none; }
		.dsk { display: block; }
		svg .at { font-size: 10.5px; }
		svg .tk, svg .xl, svg .vl { font-size: 10.5px; }
		svg .xm, svg .nn { font-size: 9.5px; }
		svg .lo { font-size: 8.5px; }
	}
</style>
