<script lang="ts">
	// The static hourglass for an outside wait. A wait inside the plan keeps the
	// circled dot, so the two kinds read apart at a glance.
	let { size = 16, title = '' }: { size?: number; title?: string } = $props();
</script>

<svg class="waiting-mark" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role={title ? 'img' : undefined} aria-label={title || undefined} aria-hidden={title ? undefined : 'true'}>
	{#if title}<title>{title}</title>{/if}
	<path d="M7 3h10M7 21h10M8 3c0 4 1.5 6 4 9-2.5 3-4 5-4 9M16 3c0 4-1.5 6-4 9 2.5 3 4 5 4 9" />
</svg>

<style>
	.waiting-mark { flex-shrink: 0; }
</style>
