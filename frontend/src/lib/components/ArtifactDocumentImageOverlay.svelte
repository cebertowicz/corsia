<script lang="ts">
	import ImageLightbox from './ImageLightbox.svelte';
	import ImageCommentLightbox from './ImageCommentLightbox.svelte';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { ImageViewportPlace } from '$lib/artifactTabs';
	import type { DocumentImageComments } from '$lib/docViewerImages';
	import type { TabDocumentImage } from '$lib/stores/tabViewState';

	let { image, comments, target, place = null, onBack, onPlace }: {
		image: TabDocumentImage;
		/** From the document under the image as it is now; none when it cannot hold comments. */
		comments?: DocumentImageComments;
		target: DocViewerTarget;
		place?: ImageViewportPlace | null;
		onBack: () => void;
		onPlace: (place: ImageViewportPlace) => void;
	} = $props();
</script>

<div class="image-overlay">
	<!-- Keyed on the tab and the image, so opening another one starts its comments afresh, and another tab's (even
	     of the same image) starts at that tab's own camera. -->
	{#key `${target.instanceId}\u0000${image.src}\u0000${image.focus?.id ?? ''}`}
		{#if comments}
			<ImageCommentLightbox
				src={image.src}
				alt={image.alt}
				artifactId={comments.artifactId}
				projectId={comments.projectId}
				readOnly={comments.readOnly}
				docName={target.name}
				{onBack}
				inDocument={{
					occurrence: { from: image.from, to: image.to },
					post: (region, text) => comments.post(image.from, image.to, region, text),
					failure: comments.failure,
				}}
				focus={image.focus ?? null}
				feedback={comments.feedback ?? null}
				{place}
				{onPlace}
			/>
		{:else}
			<ImageLightbox src={image.src} alt={image.alt} docName={target.name} {onBack} {place} {onPlace} />
		{/if}
	{/key}
</div>

<style>
	.image-overlay { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; padding: 8px 12px 12px; box-sizing: border-box; background: var(--bg-primary); }
</style>
