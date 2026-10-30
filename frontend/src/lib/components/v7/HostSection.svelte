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
	const KIND_LABEL = { lan: 'LAN', tailscale: 'Tailscale', cloud: 'Cloud' } as const;
	// How the host is reached reads as part of its description, not as a badge.
	const kind = $derived(
		group.netKind === 'lan' || group.netKind === 'tailscale' || (group.netKind === 'cloud' && !isCloud)
			? KIND_LABEL[group.netKind]
			: '',
	);
	// The subname already carries total and free; used completes it on the same line rather than repeating free beside it.
	const used = $derived(usedGb === null ? '' : `${usedGb.toFixed(0)} GB used`);
	const subline = $derived([group.subname, used, kind].filter(Boolean).join(' · '));
</script>

<section class="host-section">
	<div class="host-head" class:this={isThisMac} class:cloud={isCloud}>
		<div class="host-title">
			<h3 class="host-name">{group.name}</h3>
			{#if subline}<div class="host-subname">{subline}</div>{/if}
		</div>
	</div>

	<div class="providers">
		{@render children()}
	</div>
</section>

<style>
	/* One host is one Settings group: a section label with its description, then its rows.
	   Space alone separates hosts, as it separates groups on the other Settings pages. */
	.host-section { display: flex; flex-direction: column; gap: var(--space-1); margin-top: var(--space-6); }
	.host-section:first-of-type { margin-top: 0; }

	.host-head { padding: 0 var(--space-2); }
	.host-title { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
	.host-name { margin: 0; font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-tertiary); }
	.host-subname { margin-bottom: var(--space-1); font-size: var(--text-caption); color: var(--text-tertiary); font-variant-numeric: tabular-nums; }

	.providers { display: flex; flex-direction: column; gap: var(--space-1); }
</style>
