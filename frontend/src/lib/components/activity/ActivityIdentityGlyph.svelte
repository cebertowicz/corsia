<script lang="ts">
	/**
	 * Activity’s identity marks deliberately distinguish an agent from a
	 * conversation thread without changing the navigation drawer’s icon system.
	 */
	let { kind }: { kind: 'agent' | 'thread' } = $props();
</script>

{#if kind === 'agent'}
	<!-- Matches the canonical Agent spark used in Header and RailScopedSection. -->
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
		<path d="M12 3l1.8 5 5 1.8-5 1.8L12 17l-1.8-5-5-1.8 5-1.8z" />
	</svg>
{:else}
	<!-- A quiet conversation bubble: distinct from the Agent spark at a glance. -->
	<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
		<path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5c-1.3 0-2.6-.34-3.75-.98L4 19.5l1.42-4.04A7.47 7.47 0 0 1 5 13a7.5 7.5 0 0 1 15-1.5Z" />
		<path d="M8.5 11.5h.01M12 11.5h.01M15.5 11.5h.01" />
	</svg>
{/if}

<style>
	svg {
		width: 16px;
		height: 16px;
	}
</style>
