<script lang="ts">
	import ImageLightbox from './ImageLightbox.svelte';
	import ImageCommentLightbox from './ImageCommentLightbox.svelte';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { ImageViewportPlace } from '$lib/artifactTabs';
	import type { OpenDocumentImage } from '$lib/stores/documentImageViewer';

	let { image, target, place = null, onBack, onPlace }: {
		image: OpenDocumentImage;
		target: DocViewerTarget;
		place?: ImageViewportPlace | null;
		onBack: () => void;
		onPlace: (place: ImageViewportPlace) => void;
	} = $props();
</script>

<div class="image-overlay">
	{#if image.comments}
		{@const comments = image.comments}
		<!-- Keyed on the image, so opening another one starts its comments afresh. -->
		{#key `${image.src}\u0000${image.focus?.id ?? ''}`}
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
		{/key}
	{:else}
		<ImageLightbox src={image.src} alt={image.alt} docName={target.name} {onBack} {place} {onPlace} />
	{/if}
</div>

<style>
	.image-overlay { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; padding: 8px 12px 12px; box-sizing: border-box; background: var(--bg-primary); }
</style>
