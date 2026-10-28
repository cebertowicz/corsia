<script lang="ts">
	import { leaveImageOnExit } from '$lib/docFeedbackLifecycle.svelte';
	import ImageLightbox from './ImageLightbox.svelte';
	import ImageCommentLightbox from './ImageCommentLightbox.svelte';
	import DocBottomBand from './DocBottomBand.svelte';
	import { docComments } from '$lib/stores/docComments';
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

	// Back, Escape, another tab, a collapsed canvas or another thread all end this overlay: each is this one send of
	// the image comments the viewer still holds.
	leaveImageOnExit(() => ({ feedback: comments?.feedback, artifactId: comments?.artifactId, key: target.instanceId }));
	let bandState = $state<{ loaded: boolean; sheetModalOpen: boolean; selectedId?: string }>({ loaded: false, sheetModalOpen: false });
	let bandImage = `${target.instanceId}\u0000${image.src}`;
	$effect(() => {
		const currentImage = `${target.instanceId}\u0000${image.src}`;
		if (bandImage !== currentImage) {
			bandImage = currentImage;
			bandState = { loaded: false, sheetModalOpen: false };
		}
	});
	const unread = $derived(comments?.artifactId ? ($docComments[comments.artifactId] ?? []).filter(comment => (comment.newReplyIds?.length ?? 0) > 0 && comment.id !== bandState.selectedId) : []);
	function updateBandState(state: { loaded: boolean; sheetModalOpen: boolean; selectedId?: string }) {
		if (state.loaded) bandState = state;
	}
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
				onReplyBandState={updateBandState}
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
	{#if comments}<DocBottomBand artifactId={comments.artifactId} path="" threadId={null} threads={unread} ready={bandState.loaded} covered={false} sheetModalOpen={bandState.sheetModalOpen} onStep={() => onReplyStep(bandState.selectedId)} />{/if}
</div>

<style>
	.image-overlay { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; padding: 8px 12px 12px; box-sizing: border-box; background: var(--bg-primary); }
</style>
