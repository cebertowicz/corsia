<script lang="ts">
	import { setNetworkServerPrivate, type NetworkServer } from '$lib/stores/networkServers';
	import { saveFailureText } from '$lib/saveFailure';
	import Toggle from '../ui/Toggle.svelte';

	let { server }: { server: NetworkServer } = $props();
	// What the person just saved, until the server list reloads with the same value.
	let saved = $state<boolean | null>(null);
	const privateMachine = $derived(saved ?? server.private ?? false);
	let busy = $state(false);
	let error = $state('');
	const onThisMac = $derived(server.on_this_mac === true);
	const canMarkPrivate = $derived(!onThisMac || server.kind === 'external');
	async function savePrivate(value: boolean) {
		if (busy) return;
		busy = true; error = '';
		const result = await setNetworkServerPrivate(server.id, value);
		if (result.ok) saved = value;
		else error = saveFailureText(result.detail, result.reached);
		busy = false;
	}
</script>

<div class="stat-card">
	<div class="h">Reachable at</div>
	{#each (server.endpoints ?? []) as ep (ep.id)}
		<div class="row">
			<span class="l"><code>{ep.host}:{server.port}</code></span>
			<span class="v">
				{#if onThisMac}<span class="this-computer">This computer</span>{:else}<span class="ep-kind {ep.kind}">{ep.kind}</span>{/if}
				{#if ep.last_latency_ms != null}<span class="lat">{ep.last_latency_ms}ms</span>{/if}
			</span>
		</div>
	{/each}
</div>
{#if canMarkPrivate}
	<div class="private-machine">
		<div>
			<span>Private machine</span>
			<p>Its models count as local, so local-only chats and projects can use them. Turn this on only if the models run on that machine, not on a service it forwards to.</p>
			{#if error}<p class="status error" role="alert">{error}</p>{/if}
		</div>
		<Toggle checked={privateMachine} disabled={busy} ariaLabel="Private machine" onchange={savePrivate} />
	</div>
{/if}
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
	.this-computer { color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-regular); }
	.private-machine { display: flex; justify-content: space-between; align-items: center; gap: var(--space-3); padding: var(--space-3) 0; }
	.private-machine > div { min-width: 0; }
	.private-machine span { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-medium); }
	.private-machine p { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.private-machine .status { margin-top: var(--space-1); color: var(--red-text); }
	.private-machine :global(.tg) { flex: 0 0 auto; }
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
	.ep-kind.tailscale { color: var(--purple-text, var(--purple)); border: 1px solid var(--purple-line); background: var(--purple-bg); }
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
