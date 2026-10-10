<script lang="ts">
	import { onMount } from 'svelte';
	import { token } from '$lib/stores/auth';

	interface Props {
		serverId: string;
	}
	let { serverId }: Props = $props();

	let loading = $state(true);
	let error = $state<string | null>(null);
	let settings = $state<Record<string, any>>({});

	const LABELS: Record<string, string> = {
		host: 'Host',
		port: 'Port',
		log_level: 'Log level',
		max_process_memory: 'Max process memory',
		max_model_memory: 'Max model memory',
		max_concurrent_requests: 'Max concurrent requests',
		paged_ssd_cache_dir: 'SSD cache directory',
		paged_ssd_cache_max_size: 'SSD cache max size',
		hot_cache_max_size: 'Hot cache max size',
		initial_cache_blocks: 'Initial cache blocks',
	};

	const GROUPS: { title: string; keys: string[] }[] = [
		{ title: 'Server', keys: ['host', 'port', 'log_level'] },
		{ title: 'Memory', keys: ['max_process_memory', 'max_model_memory', 'max_concurrent_requests'] },
		{ title: 'Cache', keys: ['paged_ssd_cache_dir', 'paged_ssd_cache_max_size', 'hot_cache_max_size', 'initial_cache_blocks'] },
	];

	const knownKeys = new Set(GROUPS.flatMap(g => g.keys));
	const extras = $derived(
		Object.keys(settings).filter(k => !knownKeys.has(k)).sort(),
	);

	function labelFor(key: string): string {
		return LABELS[key] || key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
	}

	function fmt(v: any): string {
		if (v === null || v === undefined || v === '') return '—';
		if (typeof v === 'object') return JSON.stringify(v);
		return String(v);
	}

	function visibleKeys(keys: string[]): string[] {
		return keys.filter(k => k in settings);
	}

	async function load() {
		loading = true;
		error = null;
		try {
			const res = await fetch(
				`/api/settings/network-servers/${serverId}/settings?token=${$token}`,
			);
			if (res.status === 503) {
				error = 'oMLX server is unreachable. Make sure it\'s running and reachable at the configured host:port.';
				return;
			}
			if (!res.ok) {
				error = `Server returned HTTP ${res.status}`;
				return;
			}
			const data = await res.json();
			settings = (data.settings || {}) as Record<string, any>;
			if (Object.keys(settings).length === 0) {
				error = 'oMLX returned an empty settings payload. If admin access is gated, set the API key in this provider\'s connection settings.';
			}
		} catch (e: any) {
			error = e?.message ?? String(e);
		} finally {
			loading = false;
		}
	}

	onMount(load);
</script>

<div class="omlx-info">
	<p class="lede">
		Server-level oMLX flags are CLI-controlled — set when the daemon starts.
		Edit them by restarting <code>omlx serve</code> with different flags
		(<a href="https://github.com/jundot/omlx#configuration" target="_blank" rel="noreferrer">docs</a>).
		The values below are what the running daemon reports.
	</p>

	{#if loading}
		<div class="state">Loading…</div>
	{:else if error}
		<div class="state err">{error}</div>
	{:else}
		{#each GROUPS as group (group.title)}
			{@const keys = visibleKeys(group.keys)}
			{#if keys.length > 0}
				<div class="grp">
					<div class="grp-title">{group.title}</div>
					{#each keys as k (k)}
						<div class="row">
							<span class="lbl">{labelFor(k)}</span>
							<span class="val"><code>{fmt(settings[k])}</code></span>
						</div>
					{/each}
				</div>
			{/if}
		{/each}
		{#if extras.length > 0}
			<div class="grp">
				<div class="grp-title">Other</div>
				{#each extras as k (k)}
					<div class="row">
						<span class="lbl">{labelFor(k)}</span>
						<span class="val"><code>{fmt(settings[k])}</code></span>
					</div>
				{/each}
			</div>
		{/if}
	{/if}
</div>

<style>
	.omlx-info {
		display: flex;
		flex-direction: column;
		gap: 14px;
		margin-top: 8px;
	}
	.lede {
		margin: 0;
		font-size: var(--text-footnote);
		color: var(--text-secondary);
		line-height: 1.5;
	}
	.lede code {
		background: var(--surface-2);
		padding: 1px 5px;
		border-radius: var(--radius-2xs);
		font-size: var(--text-footnote);
	}
	.lede a {
		color: var(--link, var(--violet));
	}
	.state {
		font-size: var(--text-subhead);
		color: var(--text-tertiary);
	}
	.state.err {
		color: var(--err, #c0392b);
	}
	.grp {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 8px 0;
		border-top: 1px solid var(--border-subtle);
	}
	.grp:first-of-type {
		border-top: none;
		padding-top: 0;
	}
	.grp-title {
		font-size: var(--text-footnote);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-tertiary);
		margin-bottom: 2px;
	}
	.row {
		display: flex;
		align-items: baseline;
		gap: 12px;
		padding: 3px 0;
		font-size: var(--text-subhead);
	}
	.lbl {
		flex: 0 0 auto;
		min-width: 175px;
		color: var(--text-secondary);
	}
	.val {
		flex: 1 1 auto;
		color: var(--text-primary);
		overflow-wrap: anywhere;
	}
	.val code {
		font-family: var(--font-mono);
		font-size: var(--text-footnote);
		background: var(--surface-2);
		padding: 1px 5px;
		border-radius: var(--radius-2xs);
	}
</style>
