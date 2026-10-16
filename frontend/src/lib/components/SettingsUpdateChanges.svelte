<script lang="ts">
	/** A change list: its heading, the first three lines, and "Show all N" for the rest. */
	interface Props {
		heading: string;
		items: string[];
		/** Every change, including those the server summarized as `more`. */
		total: number;
	}

	const { heading, items, total }: Props = $props();
	let showAll = $state(false);
	const visible = $derived(showAll ? items : items.slice(0, 3));
</script>

<div class="changes">
	<h4>{heading}</h4>
	<ul>{#each visible as item}<li>{item}</li>{/each}</ul>
	{#if total > 3}<button class="show-all" type="button" aria-expanded={showAll} onclick={() => showAll = !showAll}>{showAll ? 'Show fewer' : `Show all ${total}`}</button>{/if}
</div>

<style>
	.changes { display: grid; gap: var(--space-2); }
	h4 { margin: 0; color: var(--text-primary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	ul { margin: 0; padding-left: var(--space-5); }
	li + li { margin-top: var(--space-1); }
	.show-all { justify-self: start; border: 0; background: transparent; padding: 0; color: var(--accent-scope-text); font: inherit; cursor: pointer; }
</style>
