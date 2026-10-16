<script lang="ts">
	import { token } from '$lib/stores/auth';
	import { formatKnownCost, type ModelEntry } from '$lib/stores/costs';
	import { get } from 'svelte/store';

	interface Props {
		providerName: string;
	}

	const { providerName }: Props = $props();

	type CostWindow = 7 | 30 | 90;
	let costWindow = $state<CostWindow>(30);
	type CostRow = ModelEntry;
	let costRows = $state<CostRow[]>([]);
	let loading = $state(false);
	let err = $state('');

	$effect(() => {
		const days = costWindow;
		const t = get(token) || '';
		(async () => {
			loading = true; err = '';
			try {
				const res = await fetch(`/api/costs/summary?token=${t}&days=${days}`);
				if (!res.ok) { err = `HTTP ${res.status}`; return; }
				const data = await res.json();
				const all: CostRow[] = data.by_model ?? [];
				costRows = all.filter(r => (r.model ?? '').startsWith(providerName + '/'));
			} catch (e) {
				err = String(e);
			} finally {
				loading = false;
			}
		})();
	});

	const total = $derived(costRows.reduce((a, b) => a + (b.cost_usd ?? 0), 0));
	const unknown = $derived(costRows.reduce((a, b) => a + (b.unknown_cost_rows ?? 0), 0));
	const max = $derived(Math.max(...costRows.map(r => r.cost_usd ?? 0), 0.0001));
	const shortName = (m: string): string => m.includes('/') ? m.split('/').slice(1).join('/') : m;
</script>

<div class="windows">
	{#each [7, 30, 90] as d (d)}
		<button type="button" class:active={costWindow === d} onclick={() => costWindow = d as CostWindow}>{d}d</button>
	{/each}
</div>

{#if loading}
	<div class="stub">Loading…</div>
{:else if err}
	<div class="stub err">Failed to load: {err}</div>
{:else if costRows.length === 0}
	<div class="stub">No usage in the last {costWindow} days.</div>
{:else}
	<div class="cost-total">
		<span class="num">{formatKnownCost(total, unknown)}</span>
		<span class="sub">last {costWindow} days</span>
	</div>
	{#each costRows as r (r.model)}
		<div class="cost-bar">
			<span class="cb-name">{shortName(r.model)}</span>
			<span class="cb-bar"><span style="width: {Math.max(2, ((r.cost_usd ?? 0) / max) * 100)}%"></span></span>
			<span class="cb-val">{formatKnownCost(r.cost_usd, r.unknown_cost_rows)}</span>
		</div>
	{/each}
{/if}

<style>
	.stub {
		font-size: var(--text-subhead); color: var(--text-tertiary);
		line-height: 1.6;
		padding: 14px 16px;
		background: var(--bg-tertiary);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		margin-top: 10px;
	}
	.stub.err {
		color: var(--red);
		border-color: color-mix(in srgb, var(--red) 30%, transparent);
		background: var(--red-bg);
	}

	.windows {
		display: flex; gap: 2px;
		background: var(--bg-tertiary);
		padding: 3px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		margin-bottom: 16px;
		width: fit-content;
	}
	.windows button {
		padding: 5px 11px;
		border-radius: var(--radius-xs);
		background: transparent; border: 0;
		color: var(--text-tertiary);
		font-size: var(--text-footnote); font-weight: 500;
		font-family: inherit; cursor: pointer;
	}
	.windows button:hover { color: var(--text-primary); }
	.windows button.active {
		background: var(--bg-elevated);
		color: var(--text-primary);
		box-shadow: 0 1px 2px rgba(0,0,0,0.3);
	}

	.cost-total {
		display: flex; align-items: baseline; gap: 8px;
		margin-bottom: 12px;
	}
	.cost-total .num {
		font-size: var(--text-title1); font-weight: 600; letter-spacing: -0.02em;
		color: var(--text-primary);
	}
	.cost-total .sub { font-size: var(--text-footnote); color: var(--text-tertiary); }

	.cost-bar {
		display: grid;
		grid-template-columns: 110px 1fr 70px;
		align-items: center;
		gap: 10px;
		margin-bottom: 6px;
		font-size: var(--text-footnote);
	}
	.cost-bar .cb-name { color: var(--text-secondary); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.cost-bar .cb-bar {
		height: 14px;
		background: var(--bg-tertiary);
		border-radius: var(--radius-2xs);
		overflow: hidden;
		box-shadow: inset 0 1px 1px rgba(0,0,0,0.3);
	}
	.cost-bar .cb-bar > span {
		display: block; height: 100%;
		background: linear-gradient(90deg, var(--accent), var(--accent-hover));
	}
	.cost-bar .cb-val {
		color: var(--text-primary);
		font-variant-numeric: tabular-nums; font-weight: 600;
		text-align: right;
	}
</style>
