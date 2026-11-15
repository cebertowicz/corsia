<!-- A Documents list row's second line: the description, then the owner. The description, with its separator, gives
way first, so the owner stays whole unless it alone is wider than the row. Where the line can't also hold one letter of
the description, its ellipsis and the separator, the description and separator leave the line together. -->
<script lang="ts">
	import { secondaryDocumentParts, type DocEntry } from '$lib/stores/docLibrary';

	let { entry }: { entry: Pick<DocEntry, 'descState' | 'description' | 'ownerName'> } = $props();
	const parts = $derived(secondaryDocumentParts(entry));
</script>

{#if entry.descState === 'pending' || entry.description || entry.ownerName}
	<span class="row-desc" class:pending={entry.descState === 'pending'}>
		{#if parts.lead}<span class="row-desc-lead"><span class="row-desc-text">{parts.lead}</span>{#if parts.owner}<span class="row-desc-sep"> · </span>{/if}</span>{/if}{#if parts.owner}<span class="row-desc-owner">{parts.owner}</span>{/if}
	</span>
{/if}

<style>
	/* One line. The owner is placed first (order -1, laid out right to left and packed to the left), so a description that
	   can't keep its 2.5em basis wraps, with its separator, onto a second line this box clips. Reading order is unchanged. */
	.row-desc { display: flex; flex-flow: row-reverse wrap; justify-content: flex-end; align-content: flex-start; height: 1lh; overflow: clip; font-size: var(--text-footnote); color: var(--text-tertiary); white-space: nowrap; }
	.row-desc-lead { display: flex; flex: 1 1 2.5em; max-width: max-content; min-width: 0; overflow: hidden; }
	.row-desc-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.row-desc-sep { flex-shrink: 0; }
	.row-desc-owner { order: -1; flex-shrink: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
	.row-desc.pending { color: var(--amber); font-style: italic; }
</style>
