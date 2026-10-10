<script lang="ts">
	import type { NetworkServer } from '$lib/stores/networkServers';

	let { server }: { server: NetworkServer } = $props();
</script>

<div class="stat-card">
	<div class="h">Reachable at</div>
	{#each (server.endpoints ?? []) as ep (ep.id)}
		<div class="row">
			<span class="l"><code>{ep.host}:{server.port}</code></span>
			<span class="v">
				<span class="ep-kind {ep.kind}">{ep.kind}</span>
				{#if ep.last_latency_ms != null}<span class="lat">{ep.last_latency_ms}ms</span>{/if}
			</span>
		</div>
	{/each}
</div>
<p class="hint">Endpoints are auto-discovered (mDNS or probe). The active host is the one currently serving requests.</p>

<style>
	.stat-card {
		background: var(--bg-tertiary);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		padding: 14px 16px;
		box-shadow: 0 1px 0 rgba(255,255,255,0.02) inset;
	}
	.h {
		display: flex; align-items: center; gap: 6px;
		font-size: var(--text-caption); font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase;
		color: var(--text-tertiary); margin-bottom: 10px;
	}
	.h::after { content: ''; flex: 1; height: 1px; background: var(--border-subtle); }
	.row {
		display: flex; justify-content: space-between; align-items: center;
		padding: 5px 0; font-size: var(--text-subhead);
		gap: 12px;
	}
	.row .l { color: var(--text-tertiary); }
	.row .v {
		color: var(--text-primary);
		font-variant-numeric: tabular-nums; font-weight: 500;
		text-align: right;
		display: inline-flex; align-items: center; gap: 8px;
	}
	.row code { font-family: 'SF Mono', monospace; font-size: var(--text-footnote); color: var(--text-secondary); }
	.hint {
		font-size: var(--text-subhead); color: var(--text-tertiary);
		line-height: 1.6;
		padding: 14px 16px;
		background: var(--bg-tertiary);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm);
		margin: 10px 0;
	}
	.ep-kind {
		font-size: var(--text-caption); font-weight: 600;
		padding: 2px 7px; border-radius: var(--radius-xs);
		text-transform: uppercase; letter-spacing: 0.04em;
	}
	.ep-kind.local     { color: var(--teal); border: 1px solid var(--teal-line); background: var(--teal-bg); }
	.ep-kind.lan       { color: var(--sky); border: 1px solid var(--sky-line); background: var(--sky-bg); }
	.ep-kind.tailscale { color: var(--violet-text, var(--violet)); border: 1px solid var(--violet-line); background: var(--violet-bg); }
	.ep-kind.public    { color: var(--text-tertiary); border: 1px solid var(--border); background: var(--bg-tertiary); }
	.lat { color: var(--text-tertiary); font-size: var(--text-footnote); font-variant-numeric: tabular-nums; }

	@media (max-width: 480px) {
		.row {
			flex-direction: column;
			align-items: flex-start;
			gap: 2px;
			padding: 6px 0;
		}
		.row .v {
			text-align: left;
			width: 100%;
			overflow-wrap: anywhere;
		}
	}
</style>
