<script lang="ts">
	interface Props {
		hiddenCount: number;
		visibleCount: number;
		availableCount: number;
		showAll: boolean;
		onShowAll: () => void;
		onShowFewer: () => void;
	}

	const { hiddenCount, visibleCount, availableCount, showAll, onShowAll, onShowFewer }: Props = $props();
</script>

{#if hiddenCount > 0}
	<button type="button" class="show-more" onclick={onShowAll}>
		Show {hiddenCount} {visibleCount === 0 ? 'available' : 'more'}
	</button>
{:else if showAll && availableCount > visibleCount}
	<button type="button" class="show-more" onclick={onShowFewer}>Show fewer</button>
{/if}

<style>
	.show-more {
		display: block; width: 100%;
		text-align: center;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		padding: calc(var(--space-2) + var(--space-1) / 2);
		border-top: 1px solid var(--border-subtle);
		background: transparent;
		border-left: 0; border-right: 0; border-bottom: 0;
		cursor: pointer;
		font-family: inherit;
		transition: all var(--dur-tight) var(--ease-snap);
	}
	.show-more:hover { color: var(--text-secondary); background: var(--bg-secondary); }
</style>
