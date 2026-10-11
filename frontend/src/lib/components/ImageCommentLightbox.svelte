<script lang="ts">
	// The image viewer with comments on parts of the image (DCOL-FR13.3-13.9):
	// numbered tags over the image, a Comment button and the comments pill in its
	// bar, and the document's own cards, composer and drawer beside it.
	import { tick } from 'svelte';
	import ImageLightbox from './ImageLightbox.svelte';
	import ImageRegionLayer from './ImageRegionLayer.svelte';
	import ImageCommentBar from './ImageCommentBar.svelte';
	import DocCommentSurface from './DocCommentSurface.svelte';
	import { createImageComments } from '$lib/imageComments.svelte';
	import { regionColor } from '$lib/imageRegions';
	import { subscribeToDocumentCommentChanges } from '$lib/docViewerOperations';
	import { commentLoads, docComments, loadComments, markThreadViewed, type DocComment } from '$lib/stores/docComments';
	import { isPaneDrawer } from '$lib/stores/paneOpen';
	import { projects } from '$lib/stores/projects';
	import { projectAccent } from '$lib/utils/projectColors';

	let { src, alt, artifactId, projectId = null, readOnly = false, draftOpen = $bindable(false), onLoaded, onFailed, onRetry }: {
		src: string;
		alt: string;
		/** Comments need the image's artifact; until it is known the viewer shows the image alone. */
		artifactId: string | null;
		projectId?: string | null;
		readOnly?: boolean;
		/** A new comment has text, so the review bar must not answer over it. */
		draftOpen?: boolean;
		onLoaded?: () => void;
		onFailed?: () => void;
		onRetry?: () => void;
	} = $props();

	let root = $state<HTMLElement>();
	let lightbox = $state<ReturnType<typeof ImageLightbox>>();
	let layer = $state<ReturnType<typeof ImageRegionLayer>>();
	const comments = $derived(artifactId ? $docComments[artifactId] ?? [] : []);
	const accent = $derived($projects.find((project) => project.id === projectId)?.accent_color ?? null);
	const colorOf = (comment: DocComment) => regionColor(comment, projectId, accent);
	const canComment = $derived(!readOnly && !!artifactId);
	const image = createImageComments({
		artifactId: () => artifactId,
		readOnly: () => !canComment,
		restoreFocus: () => root?.querySelector<HTMLElement>('.viewport')?.focus({ preventScroll: true }),
		reveal: (region) => lightbox?.revealFraction(region.x + region.w / 2, region.y + region.h / 2),
	});

	$effect(() => { draftOpen = image.hasDraftText; });
	// A new revision of the image comes with a new `src`: read the comments again, so pins placed on the old one show as stale.
	$effect(() => {
		void src;
		if (artifactId) void loadComments(artifactId).catch(() => undefined);
	});
	// An agent's comment arrives while the image is open: its pin appears, sweeping in.
	$effect(() => subscribeToDocumentCommentChanges(() => artifactId, (id) => void loadComments(id).catch(() => undefined)));

	function toggleBoxMode() {
		image.toggleBoxMode();
		// Enter on the drawing surface is the keyboard path to a whole-image comment.
		if (image.boxMode) void tick().then(() => layer?.focusSurface());
	}
	function focusFromCard(id: string) {
		if (image.ui.state?.kind === 'list') image.showFromList(id, comments.find((comment) => comment.id === id)?.region);
	}
</script>

<svelte:window onkeydowncapture={image.onKeydownCapture} />

<div class="image-comments">
<div class="layout" bind:this={root}>
	<div class="viewer-cell">
		<ImageLightbox bind:this={lightbox} {src} {alt} {onLoaded} {onFailed} {onRetry}>
			{#snippet barActions()}
				{#if artifactId}
					<ImageCommentBar
						boxMode={image.boxMode}
						moving={!!image.moving}
						savingMove={image.moveSaving}
						count={comments.length}
						loading={$commentLoads[artifactId]?.state === 'loading'}
						listOpen={image.ui.state?.kind === 'list'}
						readOnly={!canComment}
						onToggle={toggleBoxMode}
						onOpenList={image.ui.openList}
					/>
				{/if}
			{/snippet}
			{#snippet overlay({ scale })}
				{#if artifactId}
					<ImageRegionLayer
						bind:this={layer}
						{scale}
						{comments}
						{colorOf}
						draftColor={projectAccent(projectId, accent)}
						draft={image.movingRegion ?? image.draftRegion}
						selectedId={image.selectedId}
						hoveredId={image.ui.hovered}
						boxMode={image.boxMode}
						cardOpen={image.cardOpen}
						readOnly={!canComment}
						onPick={image.pick}
						onHover={image.ui.hover}
						onDraw={image.draw}
						onDismiss={image.close}
					/>
				{/if}
			{/snippet}
		</ImageLightbox>
	</div>
	{#if artifactId}
		<DocCommentSurface
			state={image.ui.state}
			anchor={image.ui.anchor}
			bounds={root}
			narrow={isPaneDrawer()}
			{comments}
			focusedId={image.selectedId}
			draft={image.draft}
			onDraftBody={image.setDraftBody}
			onCancelDraft={image.close}
			onClose={image.close}
			restoreFocus={image.ui.restoreFocus}
			onFocus={focusFromCard}
			load={$commentLoads[artifactId]}
			onRetry={() => void loadComments(artifactId).catch(() => undefined)}
			onSubmit={image.submit}
			onReply={image.threads.reply}
			onViewed={(id, replyId) => markThreadViewed(artifactId, id, replyId)}
			onResolve={image.threads.resolve}
			onAgree={image.threads.agree}
			target={null}
			{artifactId}
			readOnly={!canComment}
			image={{ src, alt, colorOf, onMovePin: image.movePin, onOpen: image.close, showThumbnail: false, imageBounds: () => lightbox?.imageBounds() ?? null }}
		/>
	{/if}
</div>
</div>

<style>
	/* The comments drawer takes the same place it does beside a document: below the image, or beside it on a wide canvas. */
	.image-comments { container: image-comments / inline-size; flex: 1; min-height: 0; display: flex; flex-direction: column; }
	.layout { flex: 1; min-height: 0; display: grid; gap: 8px; grid-template-areas: 'viewer'; grid-template-columns: minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); }
	.layout:has(:global(.canvas-comment-drawer)) { grid-template-areas: 'viewer' 'drawer'; grid-template-rows: minmax(0, 1fr) minmax(0, min(36%, 260px)); }
	@container image-comments (min-width: 900px) {
		.layout:has(:global(.canvas-comment-drawer)) { grid-template-areas: 'viewer drawer'; grid-template-columns: minmax(0, 1fr) 320px; grid-template-rows: minmax(0, 1fr); }
	}
	.viewer-cell { grid-area: viewer; min-height: 0; min-width: 0; display: flex; flex-direction: column; }
</style>
