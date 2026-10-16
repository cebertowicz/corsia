<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { HostGroup } from '$lib/stores/providersV7';

	interface Props {
		group: HostGroup;
		isThisMac?: boolean;
		isCloud?: boolean;
		children: Snippet;
	}

	const { group, isThisMac = false, isCloud = false, children }: Props = $props();

	const usedGb = $derived(group.resources ? group.resources.total_gb - group.resources.free_gb : null);
</script>

<section class="host-section">
	<div class="host-head" class:this={isThisMac} class:cloud={isCloud}>
		<div class="host-title">
			<div class="host-name">
				<span>{group.name}</span>
				{#if group.netKind === 'lan' || group.netKind === 'tailscale' || (group.netKind === 'cloud' && !isCloud)}
					<span class="net-tag {group.netKind}">{group.netKind}</span>
				{/if}
			</div>
			<div class="host-subname">{group.subname}</div>
		</div>

		{#if group.resources}
			<div class="host-meta">
				<span><strong>{usedGb?.toFixed(0)} GB</strong> used</span>
				<span class="sep">·</span>
				<span class="free">{group.resources.free_gb.toFixed(0)} GB free</span>
			</div>
		{/if}
	</div>

	<div class="host-rule"></div>

	<div class="providers">
		{@render children()}
	</div>
</section>

<style>
	.host-section { margin-bottom: 28px; }
	.host-section:last-child { margin-bottom: 0; }

	.host-head {
		display: flex; align-items: center; gap: 14px;
		padding: 6px 4px 14px;
	}

	.host-title { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
	.host-name {
		font-size: var(--text-callout); font-weight: 600; letter-spacing: -0.015em;
		color: var(--text-primary);
		display: flex; align-items: center; gap: 10px;
	}
	.host-head.this .host-name { color: var(--violet-text); }
	.host-subname {
		font-size: var(--text-footnote); color: var(--text-tertiary); font-weight: 400;
	}

	.net-tag {
		font-size: var(--text-caption); font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
		padding: 2px 7px; border-radius: var(--radius-xs);
	}
	.net-tag.localhost { color: var(--teal); border: 1px solid var(--teal-line); background: var(--teal-bg); }
	.net-tag.lan       { color: var(--sky); border: 1px solid var(--sky-line); background: var(--sky-bg); }
	.net-tag.tailscale { color: var(--violet-text); border: 1px solid var(--violet-line); background: var(--violet-bg); }
	.net-tag.cloud     { color: var(--accent-scope-text); border: 1px solid var(--accent-scope-line); background: var(--accent-scope-wash); }

	.host-meta {
		display: flex; align-items: center; gap: 12px;
		font-size: var(--text-footnote); color: var(--text-tertiary);
		font-variant-numeric: tabular-nums;
		flex-shrink: 0;
	}
	.host-meta .sep { color: var(--text-tertiary); opacity: 0.5; }
	.host-meta strong { color: var(--text-secondary); font-weight: 600; }
	.host-meta .free { color: var(--green); font-weight: 600; }

	.host-rule { height: 1px; background: var(--border-subtle); margin: 0 0 10px; }

	.providers { display: flex; flex-direction: column; gap: 4px; }

	@media (max-width: 720px) {
		.host-head { flex-wrap: wrap; gap: 10px; }
		.host-meta { width: 100%; }
	}

	@media (max-width: 480px) {
		.host-head { padding: 4px 4px 12px; gap: 10px; }
		.host-name { font-size: var(--text-body); gap: 8px; }
		.host-subname { font-size: var(--text-footnote); }
		.host-meta { font-size: var(--text-footnote); gap: 8px; }
		.host-section { margin-bottom: 20px; }
	}
</style>
