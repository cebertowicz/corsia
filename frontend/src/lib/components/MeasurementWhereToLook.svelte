<script lang="ts">
	import { agentLabel } from '$lib/stores/measurementAgentLabel';
	import type { Snippet } from 'svelte';
	import {
		formatChange,
		formatWhereToLookValue,
		type WhereToLookRow,
		type WhereToLookState,
	} from '$lib/stores/measurementWhereToLook';
	import { periodName } from '$lib/stores/measurementPeriod';

	let { state, onRetry, children }: { state: WhereToLookState; onRetry: () => void; children?: Snippet } = $props();

	const latestName = $derived(state.period ? periodName(state.period.latest) : 'the latest period');
	const previousName = $derived(state.period ? periodName(state.period.previous) : 'the previous period');
	const lists = $derived<{ id: 'worse' | 'improved'; title: string; empty: string }[]>([
		{ id: 'worse', title: 'Largest changes for the worse', empty: `Nothing got worse by 10% or more with at least 20 outcomes (or turns, or tasks) in both ${latestName} and ${previousName}.` },
		{ id: 'improved', title: 'Improved', empty: `Nothing improved by 10% or more with at least 20 outcomes (or turns, or tasks) in both ${latestName} and ${previousName}.` },
	]);
	function rows(id: 'worse' | 'improved'): WhereToLookRow[] { return state[id]; }
</script>

<section class="card" id="measurement-where-to-look" aria-labelledby="where-title">
	<div class="sh"><h2 id="where-title">Biggest changes</h2></div>
	{#if state.loading && !state.period}
		<div class="skeleton" aria-label="Loading the biggest changes"></div>
	{:else if state.error && !state.period}
		<p class="error" role="alert">{state.error}<button onclick={onRetry}>Retry</button></p>
	{:else}
		<p class="stat">
			{#if state.period}{periodName(state.period.latest)} against {periodName(state.period.previous)}, worst change first{/if}
		</p>
		{#each lists as list}
			<!-- An empty Improved list is left out; only the worse list says when nothing is in it. -->
			{#if list.id === 'worse' || rows(list.id).length}
			<h3 class:gap={list.id === 'improved'}>{list.title}</h3>
			{#if rows(list.id).length}
				<div class="tb-wrap">
				<table class="tb">
					<!-- Both lists carry the same columns, so the worse and improved tables line up on a wide screen. -->
					<colgroup><col class="c-subject" /><col class="c-measure" /><col class="c-values" /><col class="c-change" /><col class="c-n" /><col class="c-overall" /></colgroup>
					<thead><tr><th scope="col">Subject <span class="stacked">and measure</span></th><th class="measure" scope="col">Measure</th><th class="n" scope="col">Previous to latest</th><th class="n" scope="col">Change</th><th class="n" scope="col">n</th><th class="n" scope="col">Overall</th></tr></thead>
					<tbody>
						{#each rows(list.id) as row (`${row.subject.id}:${row.measure}`)}
							<tr>
								<!-- On a phone the measure column is hidden and the measure sits under the subject, in this cell. Only the worse list carries a hint, as r2.7 does. -->
								<td class="subj"><b>{row.subject.kind === 'agent' ? agentLabel(row.subject.name) : row.subject.name}</b><div class="stacked">{row.label}{#if list.id === 'worse'}<div class="act">{row.action}</div>{/if}</div></td>
								<td class="measure">{row.label}{#if list.id === 'worse'}<div class="act">{row.action}</div>{/if}</td>
								<td class="n"><span class="v">{formatWhereToLookValue(row.previous, row.format)}</span> to <span class="v">{formatWhereToLookValue(row.latest, row.format)}</span></td>
								<td class="n">{formatChange(row.change)}</td>
								<td class="n">{row.n.toLocaleString('en-US')}</td>
								<td class="n">{row.overall === null ? '–' : formatWhereToLookValue(row.overall, row.format)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
				</div>
			{:else}
				<p class="empty">{list.empty}</p>
			{/if}
			{/if}
		{/each}
		{#if state.error}<p class="error" role="alert">{state.error}<button onclick={onRetry}>Retry</button></p>
		{:else if state.loading}<p class="refreshing" aria-live="polite">Refreshing…</p>{/if}
	{/if}
	{#if children}<div class="agents-slot">{@render children()}</div>{/if}
</section>

<style>
	.card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: 0; padding: 16px; min-width: 0; }
	.sh { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin-bottom: 6px; }
	h2 { margin: 0; color: var(--text-primary); font-size: var(--text-title3); }
	.stat { margin: 0 0 12px; color: var(--text-secondary); font-size: var(--text-subhead); }
	h3 { margin: 0 0 6px; color: var(--text-secondary); font-size: var(--text-footnote); letter-spacing: 0.05em; text-transform: uppercase; }
	h3.gap { margin-top: 20px; }
	.tb-wrap { min-width: 0; overflow-x: auto; }
	.tb { width: 100%; border-collapse: collapse; color: var(--text-primary); font-size: var(--text-subhead); }
	.tb th, .tb td { text-align: left; padding: 6px 4px; border-top: 1px solid var(--border-subtle); vertical-align: top; }
	/* A subject wraps between whole words only, as in r2.7; the column is at least as wide as its longest word. */
	.act { overflow-wrap: anywhere; }
	.tb td:first-child, .tb th:first-child { min-width: 10ch; }
	.tb th { border-top: 0; vertical-align: bottom; color: var(--text-secondary); font-size: var(--text-footnote); font-weight: 700; }
	.tb td.n, .tb th.n { text-align: right; font-variant-numeric: tabular-nums; }
	.tb td b { font-weight: 700; }
	.act { margin-top: 2px; color: var(--text-secondary); font-size: var(--text-footnote); font-weight: 400; }
	.v { white-space: nowrap; }
	.stacked { display: none; }
	/* Wide: fixed column widths, the same in both tables, so the worse and improved lists line up. */
	@media (min-width: 1024px) {
		.tb { table-layout: fixed; }
		.c-subject { width: 18%; }
		.c-measure { width: 34%; }
		.c-values { width: 18%; }
		.c-change { width: 10%; }
		.c-n { width: 8%; }
		.c-overall { width: 12%; }
	}
	@media (max-width: 480px) {
		.card { padding: 12px; }
		.tb { font-size: var(--text-footnote); }
		.tb th, .tb td { padding: 6px 3px; }
		.tb th { font-size: var(--text-caption); }
		.act { font-size: 11px; }
	}
	/* Phone: the subject and its measure share one cell, the subject above, so neither wraps a word per line.
	   No column keeps a width floor, so the table always fits the card; a subject or header breaks only between
	   words, except a single unbroken name wider than the card, which may break anywhere rather than push the page. */
	@media (max-width: 430px) {
		.tb td:first-child, .tb th:first-child { min-width: 0; }
		.tb td.subj b { overflow-wrap: anywhere; }
		.tb th { white-space: normal; word-break: normal; }
		.tb th.measure, .tb td.measure { display: none; }
		span.stacked { display: inline; }
		div.stacked { display: block; margin-top: 4px; }
	}
	.agents-slot { margin-top: 20px; min-width: 0; }
	.empty, .refreshing { margin: 0; color: var(--text-secondary); font-size: var(--text-subhead); }
	.empty { font-style: italic; }
	/* Phone: the period line and the empty line keep r2.7's 13px, like the table, against the larger phone type scale. */
	@media (max-width: 480px) {
		.stat, .empty { font-size: var(--text-footnote); }
	}
	.error { color: var(--red-text); display: flex; align-items: center; gap: 8px; }
	.error button { border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); background: var(--bg-primary); color: var(--text-primary); padding: 4px 8px; cursor: pointer; }
	.skeleton { height: 96px; border-radius: var(--radius-xs); background: var(--bg-hover); animation: pulse 1.2s ease-in-out infinite alternate; }
	@keyframes pulse { to { opacity: 0.55; } }
</style>
