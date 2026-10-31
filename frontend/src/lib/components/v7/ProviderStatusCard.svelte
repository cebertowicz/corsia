<script lang="ts">
	import type { HostProvider } from '$lib/stores/providersV7';
	import { ignoredEnvironmentNote } from '$lib/stores/keys';

	let { provider }: { provider: HostProvider } = $props();
	const hasServer = $derived(!!provider.server);
</script>

<div class="stat-card">
	<div class="h">Status</div>
	{#if provider.type === 'cloud'}
		<div class="row"><span class="l">API key</span><span class="v">{provider.raw.masked_key ?? 'not set'}</span></div>
		<div class="row"><span class="l">Models</span><span class="v">{provider.raw.models?.length ?? 0}</span></div>
		<div class="row"><span class="l">Base URL</span><span class="v"><code>{provider.raw.api_base ?? 'default'}</code></span></div>
		{#if provider.raw.ignored_env_var}
			<p class="note" data-ignored-env>{ignoredEnvironmentNote(provider.raw.ignored_env_var)}</p>
		{/if}
	{:else if hasServer && provider.server}
		<div class="row"><span class="l">Server</span><span class="v">{provider.server.status}</span></div>
		<div class="row"><span class="l">Port</span><span class="v">{provider.server.port}</span></div>
		<div class="row"><span class="l">Active host</span><span class="v"><code>{provider.server.active_host ?? '-'}</code></span></div>
		{#if provider.server.memory_total_mb}
			<div class="row"><span class="l">Model memory</span><span class="v">{(provider.server.memory_total_mb / 1024).toFixed(0)} GB total · {((provider.server.memory_used_mb ?? 0) / 1024).toFixed(0)} GB used</span></div>
		{/if}
	{/if}
</div>

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
	.note { margin: 8px 0 0; font-size: var(--text-footnote); color: var(--text-secondary); }
	.row code { font-family: 'SF Mono', monospace; font-size: var(--text-footnote); color: var(--text-secondary); }

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
