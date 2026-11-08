<script lang="ts">
	import { leaveImageOnExit } from '$lib/docFeedbackLifecycle.svelte';
	import { get } from 'svelte/store';
	import { onDestroy } from 'svelte';
	import { releaseDiagramSnapshot } from '$lib/diagramSnapshot';
	import { refreshTabDocumentImage } from '$lib/stores/tabViewState';
	import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';
	import ImageLightbox from './ImageLightbox.svelte';
	import ImageCommentLightbox from './ImageCommentLightbox.svelte';
	import DocBottomBand from './DocBottomBand.svelte';
	import { docComments, hasUnread } from '$lib/stores/docComments';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { ImageViewportPlace } from '$lib/artifactTabs';
	import type { DocumentImageComments } from '$lib/docViewerImages';
	import type { TabDocumentImage } from '$lib/stores/tabViewState';

	let { image, comments, target, place = null, onBack, onPlace, onReplyJump }: {
		image: TabDocumentImage;
		/** From the document under the image as it is now; none when it cannot hold comments. */
		comments?: DocumentImageComments;
		target: DocViewerTarget;
		place?: ImageViewportPlace | null;
		onBack: () => void;
		onPlace: (place: ImageViewportPlace) => void;
		/** Goes to one unread thread the jump control is pointing at: its pin, or its text once the image is closed. */
		onReplyJump: (id: string) => void;
	} = $props();

	let destroyed = false;
	onDestroy(() => { destroyed = true; });
	async function refreshDiagram(): Promise<boolean> {
		if (!comments || image.diagramSource === undefined || !target.threadId) return false;
		const opened = image;
		const next = await comments.refreshDiagram(opened.from, opened.to);
		if (!next) return false;
		if (destroyed || next.diagramSource === opened.diagramSource) {
			releaseDiagramSnapshot(next.src);
			return false;
		}
		return refreshTabDocumentImage(target.threadId, target.instanceId, opened, next);
	}
	let hasDraft = $state(false);
	let sheetUp = $state(false);
	let selectedThread = $state<string | null>(null);
	let loaded = $state(false);
	let overlayRoot = $state<HTMLElement>();
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

<div class="image-overlay" bind:this={overlayRoot}>
	<!-- Keyed on this opening of the image, so opening another one, or the same one again, starts its comments afresh,
	     and another tab's (even of the same image) starts at that tab's own camera. -->
	{#key image.viewId}
		{#if comments}
			<ImageCommentLightbox
				bind:this={viewer}
				src={image.src}
				alt={image.alt}
				readableScale={image.readableScale}
				diagramElements={image.diagramElements}
				diagramSource={image.diagramSource}
				{refreshDiagram}
				artifactId={comments.artifactId}
				{target}
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
					post: (region, text, taggedAgentIds, mentions) => comments.post(image.from, image.to, region, text, taggedAgentIds, mentions, image.diagramSource),
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
			<DocBottomBand artifactId={comments.artifactId} threads={unread} root={overlayRoot} ready={loaded} covered={hasDraft} sheetModalOpen={sheetUp} ignoreConfirmationId={priorConfirmation} onJump={(thread) => onReplyJump(thread.id)} />
		</div>
	{/if}
</div>

<style>
	.image-overlay { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; padding: var(--space-2) var(--space-3) var(--space-3); box-sizing: border-box; background: var(--bg-primary); }
	.viewer-band { position: absolute; bottom: var(--space-3); left: var(--space-3); right: var(--space-3); pointer-events: none; }
	.viewer-band :global([data-doc-bottom-band]) { pointer-events: auto; }
</style>
