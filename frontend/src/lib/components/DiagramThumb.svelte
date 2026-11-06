<script lang="ts">
	// The comments drawer's row for one diagram: the image row (`ImageRegionThumb`) over the diagram as the document draws
	// it now, current theme and current source, with the listed pins at their stored spots. It is drawn from the source here, as the
	// document's own widget draws it, so a diagram that is off screen, or was edited after the pins were placed, still shows.
	import type { DocComment } from '$lib/stores/docComments';
	import { diagramMarkupSnapshot } from '$lib/cm/widget-open';
	import { renderMermaid } from '$lib/mermaid';
	import { releaseDiagramSnapshot } from '$lib/diagramSnapshot';
	import ImageRegionThumb from './ImageRegionThumb.svelte';

	let { source, caption, comments, colorOf, onOpen }: {
		source: string;
		caption: string;
		comments: DocComment[];
		colorOf: (comment: DocComment) => string;
		onOpen?: () => void;
	} = $props();

	let src = $state<string | null>(null);
	$effect(() => {
		let current = true, url: string | null = null;
		void renderMermaid(source).then((result) => {
			if (!current || !result.ok) return;
			url = diagramMarkupSnapshot(result.svg);
			src = url;
		});
		return () => { current = false; src = null; releaseDiagramSnapshot(url); };
	});
</script>

{#if src}
	<ImageRegionThumb {src} alt={caption} caption kind="diagram" {comments} {colorOf} {onOpen} />
{:else}
	<!-- Until it has drawn, or when it does not draw: the caption an image's thumbnail carries. -->
	<button type="button" class="diagram-head" data-diagram-head disabled={!onOpen} onclick={() => onOpen?.()}>{caption}</button>
{/if}

<style>
	.diagram-head { display: block; width: 100%; padding: calc(var(--space-1) / 2) 0 var(--chip-inline-padding); border: 0; background: none; font: inherit; font-size: var(--text-caption); line-height: 1.35; color: var(--text-secondary); text-align: start; cursor: pointer; }
	.diagram-head:disabled { cursor: default; }
	.diagram-head:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
</style>
