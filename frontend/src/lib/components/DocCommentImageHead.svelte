<script lang="ts">
	import ImageRegionThumb from './ImageRegionThumb.svelte';
	import DiagramThumb from './DiagramThumb.svelte';
	import { imageAlt, onItsImage, type DocImageComments } from '$lib/docImageComments';
	import type { DocComment } from '$lib/stores/docComments';
	let { row, images }: { row: unknown[]; images: DocImageComments | null } = $props();
	const group = $derived(row as DocComment[]);
	const src = $derived(images?.srcOf(group[0]));
</script>

{#if images && src}<ImageRegionThumb {src} alt={imageAlt(group[0])} caption comments={group} colorOf={images.colorOf} onOpen={onItsImage(group[0]) ? () => images?.onOpen(group[0]) : undefined} />
{:else if images && images.diagramOf?.(group[0])}
	{@const diagram = images.diagramOf?.(group[0])}
	<DiagramThumb source={diagram?.source ?? ''} caption={diagram?.caption ?? 'Diagram'} comments={group} colorOf={images.colorOf} onOpen={onItsImage(group[0]) ? () => images?.onOpen(group[0]) : undefined} />{/if}
