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
	/* A text action, like the offer's "Show all". */
	.show-more {
		display: block;
		margin-left: calc(var(--space-2) + var(--project-mark-size) + var(--space-3));
		margin-right: var(--space-2);
		padding: var(--space-1) 0;
		border: 0; background: none;
		color: var(--accent-text);
		font: inherit; font-size: var(--text-footnote);
		cursor: pointer;
	}
	.show-more:hover { text-decoration: underline; }
	.show-more:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
</style>
