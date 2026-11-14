<!-- A Documents list row's second line: the description, then the owner. The description, with its separator, gives
way first, so the owner stays whole unless it alone is wider than the row. -->
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
	.row-desc { display: flex; font-size: var(--text-footnote); color: var(--text-tertiary); overflow: hidden; white-space: nowrap; }
	.row-desc-lead { display: flex; min-width: 0; overflow: hidden; }
	.row-desc-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.row-desc-sep { flex-shrink: 0; }
	.row-desc-owner { flex-shrink: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
	.row-desc.pending { color: var(--amber); font-style: italic; }
</style>
