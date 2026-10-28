<script lang="ts">
	// The image viewer with comments on parts of the image (DCOL-FR13.3-13.9):
	// numbered tags over the image, a Comment button and the comments pill in its
	// bar, and the document's own cards, composer and drawer beside it. An image
	// inside a document shows the document's comments on that one image.
	import { onMount, tick } from 'svelte';
	import ImageLightbox from './ImageLightbox.svelte';
	import ImageRegionLayer from './ImageRegionLayer.svelte';
	import ImageCommentBar from './ImageCommentBar.svelte';
	import DocCommentSurface from './DocCommentSurface.svelte';
	import DocBottomBand from './DocBottomBand.svelte';
	import DocFeedbackControls from './DocFeedbackControls.svelte';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import type { ImageViewportPlace } from '$lib/artifactTabs';
	import { createImageComments } from '$lib/imageComments.svelte';
	import { commentsAtImage, type ImageOccurrence } from '$lib/docImageComments';
	import type { ImageRegion } from '$lib/imageRegions';
	import { regionColor, revealCentre } from '$lib/imageRegions';
	import { FIT_MARGIN } from '$lib/imageZoom';
	import { subscribeToDocumentCommentChanges } from '$lib/docViewerOperations';
	import { commentLoads, docComments, loadComments, markThreadViewed, type DocComment } from '$lib/stores/docComments';
	import { isPaneDrawer } from '$lib/stores/paneOpen';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { projects } from '$lib/stores/projects';
	import { projectAccent } from '$lib/utils/projectColors';

	let { src, alt, artifactId, projectId = null, readOnly = false, docName = '', onBack, inDocument = null, focus = null, feedback = null, draftOpen = $bindable(false), place = null, onPlace, onLoaded, onFailed, onRetry, onReplyStep }: {
		src: string;
		alt: string;
		/** Comments need the image's artifact (a document's, for an image in it); until it is known the viewer shows the image alone. */
		artifactId: string | null;
		projectId?: string | null;
		readOnly?: boolean;
		/** The document the image is in, and the way back to it. */
		docName?: string;
		onBack?: () => void;
		/** An image inside a document: where its markdown is, and how a comment anchors on it. */
		inDocument?: {
			occurrence: ImageOccurrence;
			post: (region: ImageRegion, text: string) => Promise<string>;
			failure?: (error: unknown) => string;
		} | null;
		/** Opened on this comment: its card shows at its tag, or with `move` its pin is placed again. */
		focus?: { id: string; move?: boolean } | null;
		/**
		 * The batch the bar shows pending-send status and Send for (DCOL-FR13.12): the document's, for an image inside
		 * one (so going Back is not leaving it), or an image artifact's own, held by its tab (so a reload is not either).
		 */
		feedback?: DocFeedback | null;
		/** A new comment has text, so the review bar must not answer over it. */
		draftOpen?: boolean;
		place?: ImageViewportPlace | null;
		onPlace?: (place: ImageViewportPlace) => void;
		onLoaded?: () => void;
		onFailed?: () => void;
		onRetry?: () => void;
		onReplyStep?: (excludeId?: string) => void;
	} = $props();

	let root = $state<HTMLElement>();
	let lightbox = $state<ReturnType<typeof ImageLightbox>>();
	let layer = $state<ReturnType<typeof ImageRegionLayer>>();
	const all = $derived(artifactId ? $docComments[artifactId] ?? [] : []);
	const comments = $derived(inDocument ? commentsAtImage(all, inDocument.occurrence) : all);
	// The pill counts open threads, as the document's does: resolved ones are in the drawer's other filters.
	const openThreads = $derived(comments.filter((comment) => !comment.resolved).length);
	const accent = $derived($projects.find((project) => project.id === projectId)?.accent_color ?? null);
	const colorOf = (comment: DocComment) => regionColor(comment, projectId, accent);
	const canComment = $derived(!readOnly && !!artifactId);
	function reload(id: string) {
		return loadComments(id).then((current) => feedback?.sync(id, current)).catch(() => undefined);
	}
	/** Reads the comments (and their batch) again: the user came back to the browser tab. */
	export function refresh() {
		if (artifactId) return reload(artifactId);
	}
	const image = $derived.by(() => {
		void focus?.id;
		return createImageComments({
			artifactId: () => artifactId,
			readOnly: () => !canComment,
			restoreFocus: () => root?.querySelector<HTMLElement>('.viewport')?.focus({ preventScroll: true }),
			reveal: (id, region) => {
				const frame = root?.querySelector('[data-image-regions]')?.getBoundingClientRect();
				if (!frame?.width || !frame.height) return;
				const tag = root?.querySelector(`[data-region-tag][data-comment-id="${id}"]:not([inert])`)?.getBoundingClientRect() ?? null;
				const centre = revealCentre(region, tag, frame);
				lightbox?.revealFraction(centre.x, centre.y);
			},
			post: () => (inDocument ? { create: inDocument.post, failure: inDocument.failure } : null),
			onCreated: () => {
				feedback?.commentCreated();
				if (artifactId) reload(artifactId);
			},
		});
	});

	// Opened from a comment in the document: once the image has drawn, its card shows at its tag, once.
	let focusedId: string | undefined;
	async function showFocus() {
		const wanted = focus;
		if (!wanted || focusedId === wanted.id) return;
		await tick();
		const comment = comments.find((item) => item.id === wanted.id);
		if (!comment) return;
		if (focus?.id !== wanted.id) return;
		if (wanted.move) { focusedId = wanted.id; image.movePin(wanted.id); return; }
		const tag = root?.querySelector<HTMLElement>(`[data-region-tag][data-comment-id="${wanted.id}"]`);
		if (!tag) return;
		focusedId = wanted.id;
		image.select(wanted.id, tag, comment.region);
	}
	let loaded = $state(false);
	let loadedFocus = $state<string | null | undefined>(undefined);
	function imageLoaded() {
		loaded = true;
		loadedFocus = focus?.id ?? null;
		onLoaded?.();
	}
	$effect(() => { if (loaded && loadedFocus === (focus?.id ?? null) && comments.length) void showFocus(); });

	$effect(() => { draftOpen = image.hasDraftText; });
	$effect(() => {
		const currentRoot = root;
		const id = artifactId;
		const controller = feedback;
		if (!currentRoot || !id || !controller || controller.imageHeld === 0) return;
		const touch = () => controller.touchImage();
		for (const event of ['pointermove', 'pointerdown', 'keydown', 'wheel']) {
			currentRoot.addEventListener(event, touch, { passive: true });
		}
		return () => {
			for (const event of ['pointermove', 'pointerdown', 'keydown', 'wheel']) currentRoot.removeEventListener(event, touch);
		};
	});
	// A new revision of the image comes with a new `src`: read the comments again, so pins placed on the old one show as stale.
	$effect(() => {
		void src;
		if (artifactId) reload(artifactId);
	});
	// An agent's comment arrives while the image is open: its pin appears, sweeping in.
	$effect(() => subscribeToDocumentCommentChanges(() => artifactId, reload));

	function toggleBoxMode() {
		image.toggleBoxMode();
		// Enter on the drawing surface is the keyboard path to a whole-image comment.
		if (image.boxMode) void tick().then(() => layer?.focusSurface());
	}
	function focusFromCard(id: string) {
		if (image.ui.state?.kind === 'list') image.showFromList(id, comments.find((comment) => comment.id === id)?.region);
	}

	// The drawer opens by growing its grid track, so the image above or beside it moves with it rather than jumping.
	const drawerOpen = $derived(image.ui.state?.kind === 'list');
	/**
	 * Under reduced motion nothing glides: the drawer crossfades, in a sheet or beside the image, and the image holds its
	 * place until the drawer has gone, then moves once. So the track stays open while the drawer is still on screen.
	 */
	let drawerShown = $state(false);
	const trackOpen = $derived(drawerOpen || ($prefersReducedMotion && drawerShown));
	const DRAWER_GAP = 8;
	let drawerSize = $state(0);
	/**
	 * Below the image the drawer takes what the image does not need at Fit, and never less than the usual share, so a
	 * short screenshot leaves room for several cards (F6); a tall one keeps the usual split and fits smaller.
	 */
	function measureDrawer() {
		if (!root) return;
		const box = root.getBoundingClientRect();
		const bar = root.querySelector('.bar')?.getBoundingClientRect().height ?? 0;
		const usual = Math.min(box.height * 0.36, 260);
		const natural = lightbox?.naturalSize();
		if (!natural || !(natural.width > 0)) { drawerSize = Math.round(usual); return; }
		const fitted = natural.height * Math.min(1, Math.max(0, box.width - 2 * FIT_MARGIN) / natural.width);
		drawerSize = Math.round(Math.max(usual, box.height - DRAWER_GAP - (bar + fitted + 2 * FIT_MARGIN)));
	}
	$effect(() => { if (drawerOpen) measureDrawer(); });
	onMount(() => {
		const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => { if (drawerOpen) measureDrawer(); }) : null;
		if (root) observer?.observe(root);
		return () => observer?.disconnect();
	});
</script>

<svelte:window onkeydowncapture={image.onKeydownCapture} />

<div class="image-comments">
<div class="layout" class:drawer-open={trackOpen} style:--drawer-size="{drawerSize}px" bind:this={root}>
	<div class="viewer-cell">
		{#key focus?.id}
		<ImageLightbox bind:this={lightbox} {src} {alt} {docName} {onBack} {place} {onPlace} onLoaded={imageLoaded} {onFailed} {onRetry}>
			{#snippet barActions({ loaded })}
				{#if artifactId}
					<ImageCommentBar
						boxMode={image.boxMode}
						ready={loaded}
						moving={!!image.moving}
						savingMove={image.moveSaving}
						count={openThreads}
						unsent={feedback?.pendingCount ?? 0}
						loading={$commentLoads[artifactId]?.state === 'loading'}
						listOpen={image.ui.state?.kind === 'list'}
						readOnly={!canComment}
						onToggle={toggleBoxMode}
						onOpenList={image.ui.openList}
					/>
					<!-- The bar is narrow at any window size (a canvas panel beside the chat): the image lane's Send is the one control here, so leaving the image and pressing it send the same comments. -->
					{#if feedback}
						<DocFeedbackControls {feedback} compact lane="image-held" />
					{/if}
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
						draft={image.draftRegion}
						moving={image.moving ? { id: image.moving, region: image.movingRegion } : null}
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
		{/key}
		{#if onReplyStep}<DocBottomBand {artifactId} path="" threadId={null} threads={all.filter((comment) => (comment.newReplyIds?.length ?? 0) > 0 && !(image.ui.state?.kind === 'thread' && image.ui.state.id === comment.id))} ready={loaded} covered={false} sheetModalOpen={image.ui.sheetModalOpen} onStep={() => onReplyStep?.(image.ui.state?.kind === 'thread' ? image.ui.state.id : undefined)} />{/if}
	</div>
	{#key focus?.id}
	{#if artifactId}
		<!-- The drawer lists this one image's comments while the image itself is on screen above or beside it, so it has no thumbnail group header repeating the image. -->
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
			onDrawerShown={(shown) => drawerShown = shown}
			onSheetModalChange={(open) => image.ui.sheetModalOpen = open}
			image={{ src, alt, colorOf, onMovePin: image.movePin, onOpen: image.close, showThumbnail: false, imageBounds: () => lightbox?.imageBounds() ?? null, hoveredId: image.ui.hovered, onHover: image.ui.hover, picks: image.listPicks }}
		/>
	{/if}
	{/key}
</div>
</div>

<style>
	/*
	 * The comments drawer takes the same place it does beside a document: below the image, or beside it on a wide canvas.
	 * Its track grows from nothing, and the drawer rides its leading edge, clipped by the layout, so it slides in from the
	 * bottom (or the right) while the image refits in what is left. Reduced motion skips the slide; the drawer crossfades.
	 */
	.image-comments { container: image-comments / inline-size; flex: 1; min-height: 0; display: flex; flex-direction: column; }
	.layout {
		/* Measured on open and set inline; nothing until then. */
		--drawer-size: 0px;
		flex: 1; min-height: 0; display: grid; gap: 0; overflow: clip;
		grid-template-areas: 'viewer' 'drawer'; grid-template-columns: minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) 0px;
		transition: grid-template-rows calc(var(--dur-drawer) * 0.7) var(--ease-ios-sheet), grid-template-columns calc(var(--dur-drawer) * 0.7) var(--ease-ios-sheet);
	}
	.layout.drawer-open { grid-template-rows: minmax(0, 1fr) var(--drawer-size); transition-duration: var(--dur-drawer); }
	.layout :global(.canvas-comment-drawer) { align-self: start; box-sizing: border-box; height: calc(var(--drawer-size) - 8px); margin-top: 8px; }
	@container image-comments (min-width: 900px) {
		.layout { grid-template-areas: 'viewer drawer'; grid-template-columns: minmax(0, 1fr) 0px; grid-template-rows: minmax(0, 1fr); }
		.layout.drawer-open { grid-template-columns: minmax(0, 1fr) 328px; grid-template-rows: minmax(0, 1fr); }
		.layout :global(.canvas-comment-drawer) { align-self: stretch; justify-self: start; width: 320px; height: auto; margin: 0 0 0 8px; }
	}
	/* Reduced motion skips the slide: the track opens at once, and closes at once when the drawer has gone (trackOpen). */
	@media (prefers-reduced-motion: reduce) { .layout { transition: none; } }
	.viewer-cell { grid-area: viewer; min-height: 0; min-width: 0; display: flex; flex-direction: column; }
</style>
