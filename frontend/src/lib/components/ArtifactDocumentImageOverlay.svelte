<script lang="ts">
	import { leaveImageOnExit } from '$lib/docFeedbackLifecycle.svelte';
	import { get } from 'svelte/store';
	import { onDestroy } from 'svelte';
	import type { OpenImageRequest } from '$lib/cm/markdown-image';
	import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';
	import ImageLightbox from './ImageLightbox.svelte';
	import ImageCommentLightbox from './ImageCommentLightbox.svelte';
	import DocBottomBand from './DocBottomBand.svelte';
	import { docComments, hasUnread } from '$lib/stores/docComments';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { ImageViewportPlace } from '$lib/artifactTabs';
	import type { DocumentImageComments } from '$lib/docViewerImages';
	import type { TabDocumentImage } from '$lib/stores/tabViewState';

	let { image, comments, target, place = null, onBack, onPlace, onReplyStep }: {
		image: TabDocumentImage;
		/** From the document under the image as it is now; none when it cannot hold comments. */
		comments?: DocumentImageComments;
		target: DocViewerTarget;
		place?: ImageViewportPlace | null;
		onBack: () => void;
		onPlace: (place: ImageViewportPlace) => void;
		onReplyStep: (excludeId?: string) => void;
	} = $props();

	let refreshed = $state<OpenImageRequest | null>(null);
	const currentImage = $derived(refreshed ? { ...image, ...refreshed, from: image.from, to: image.to } : image);
	async function refreshDiagram(): Promise<boolean> {
		if (!comments || image.diagramSource === undefined) return false;
		const next = await comments.refreshDiagram(image.from, image.to);
		if (!next) return false;
		if (next.diagramSource === currentImage.diagramSource) {
			if (next.src.startsWith('blob:')) URL.revokeObjectURL(next.src);
			return false;
		}
		if (refreshed?.src.startsWith('blob:')) URL.revokeObjectURL(refreshed.src);
		refreshed = next;
		return true;
	}
	onDestroy(() => { if (refreshed?.src.startsWith('blob:')) URL.revokeObjectURL(refreshed.src); });
	let hasDraft = $state(false);
	let sheetUp = $state(false);
	let selectedThread = $state<string | null>(null);
	let loaded = $state(false);
	let bottomBand = $state<HTMLDivElement>();
	let viewer = $state<ReturnType<typeof ImageCommentLightbox>>();
	// A document confirmation already showing belongs to the document underneath this viewer.
	const priorConfirmation = get(deliveryConfirmation)?.id ?? null;
	$effect(() => {
		const band = bottomBand;
		if (!band || typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(() => viewer?.refreshCoveredArea());
		observer.observe(band);
		return () => observer.disconnect();
	});
	const unread = $derived(comments?.artifactId
		? ($docComments[comments.artifactId] ?? []).filter((comment) => hasUnread(comment) && comment.id !== selectedThread)
		: []);

	// Back, Escape, another tab, a collapsed canvas or another thread all end this overlay: each is this one send of
	// the image comments the viewer still holds.
	leaveImageOnExit(() => ({ feedback: comments?.feedback, artifactId: comments?.artifactId, key: target.instanceId }));
</script>

<div class="image-overlay">
	<!-- Keyed on the tab and the image, so opening another one starts its comments afresh, and another tab's (even
	     of the same image) starts at that tab's own camera. -->
	{#key `${target.instanceId}\u0000${image.src}\u0000${image.focus?.id ?? ''}`}
		{#if comments}
			<ImageCommentLightbox
				bind:this={viewer}
				src={currentImage.src}
				alt={currentImage.alt}
				readableScale={currentImage.readableScale}
				diagramElements={currentImage.diagramElements}
				diagramSource={currentImage.diagramSource}
				{refreshDiagram}
				artifactId={comments.artifactId}
				projectId={comments.projectId}
				readOnly={comments.readOnly}
				docName={target.name}
				docPath={target.path}
				{onBack}
				bind:hasDraft
				bind:sheetUp
				bind:selectedThread
				onLoaded={() => loaded = true}
				inDocument={{
					occurrence: { from: image.from, to: image.to },
					post: (region, text) => comments.post(image.from, image.to, region, text, currentImage.diagramSource),
					failure: comments.failure,
				}}
				focus={image.focus ?? null}
				feedback={comments.feedback ?? null}
				visibleBottom={() => bottomBand?.querySelector('[data-doc-bottom-band]')?.getBoundingClientRect().top ?? null}
				{place}
				{onPlace}
			/>
		{:else}
			<ImageLightbox src={image.src} alt={image.alt} readableScale={image.readableScale} docName={target.name} {onBack} {place} {onPlace} />
		{/if}
	{/key}
	{#if comments}
		<div class="viewer-band" bind:this={bottomBand}>
			<DocBottomBand artifactId={comments.artifactId} threads={unread} ready={loaded} covered={hasDraft} sheetModalOpen={sheetUp} ignoreConfirmationId={priorConfirmation} onStep={() => onReplyStep(selectedThread ?? undefined)} />
		</div>
	{/if}
</div>

<style>
	.image-overlay { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; padding: var(--space-2) var(--space-3) var(--space-3); box-sizing: border-box; background: var(--bg-primary); }
	.viewer-band { position: absolute; bottom: var(--space-3); left: var(--space-3); right: var(--space-3); pointer-events: none; }
	.viewer-band :global([data-doc-bottom-band]) { pointer-events: auto; }
</style>
