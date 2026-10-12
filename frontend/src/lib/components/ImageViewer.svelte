<script lang="ts">
	// Renders a workspace image file inside the artifact leaf, with comments on
	// parts of it and no editor. The bytes come from the authenticated file
	// route and are only ever shown through <img> (inside the lightbox, which
	// zooms and pans it), so an SVG can never run script or reach the DOM.
	import './DocViewer.css';
	import ImageCommentLightbox from './ImageCommentLightbox.svelte';
	import ReviewBar from './ReviewBar.svelte';
	import { untrack } from 'svelte';
	import { fetchRevision } from '$lib/api/docs';
	import { imageContentUrl } from '$lib/artifactImage';
	import { holdDocFeedback } from '$lib/docFeedback.svelte';
	import { token } from '$lib/stores/auth';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { TabLoadSignal } from '$lib/artifactTabs';

	let { target, onSignal, stale = false, onReload }: {
		target: DocViewerTarget;
		/** Reports load progress against the tab instance that started it. */
		onSignal?: (signal: TabLoadSignal) => void;
		/** A newer revision exists than the one on screen. */
		stale?: boolean;
		onReload?: () => void;
	} = $props();

	type ImageState = 'loading' | 'ready' | 'error';

	let imageState = $state<ImageState>('loading');
	let src = $state<string | null>(null);
	let revision = $state<string | null>(null);
	let resolvedArtifactId = $state<string | null>(null);
	let reloadNonce = $state(0);
	let loadSeq = 0;
	let draftOpen = $state(false);
	// The review bar reports how much of the viewer its floating phone capsule covers.
	let capsuleClearance = $state(0);
	let lightbox = $state<ReturnType<typeof ImageCommentLightbox>>();
	const artifactId = $derived(resolvedArtifactId ?? target.artifactId ?? null);
	// The image's own batch is held by its tab, not the lightbox: a reload remounts that, and is not leaving.
	const feedback = holdDocFeedback(() => ({ artifactId, targetInstanceId: target.instanceId }), () => lightbox?.refresh());

	export function reload() {
		reloadNonce += 1;
	}

	// Keyed off the load-relevant fields only, so a sibling tab's status update
	// that recreates the `target` object never restarts this load.
	const targetKey = $derived(`${target.instanceId}\u0000${target.path}\u0000${target.projectId ?? ''}`);

	$effect(() => {
		targetKey;
		reloadNonce;
		untrack(() => {
			const d = target;
			const seq = ++loadSeq;
			const signal = (s: Omit<TabLoadSignal, 'instanceId'>) => onSignal?.({ ...s, instanceId: d.instanceId });
			imageState = 'loading';
			src = null;
			signal({ status: 'loading' });
			fetchRevision(d.path, d.projectId, d.artifactId)
				.then((probe) => {
					if (seq !== loadSeq) return;
					if (!probe.exists) {
						signal({ status: 'unavailable' });
						return;
					}
					// The file stopped being an image since this tab chose its viewer: hand it back to the document one.
					if (!probe.media_type) {
						signal({ status: 'loading', renderer: 'document' });
						return;
					}
					revision = probe.revision;
					resolvedArtifactId = probe.artifact_id ?? d.artifactId;
					src = imageContentUrl({
						path: probe.path ?? d.path,
						projectId: d.projectId,
						token: $token ?? '',
						revision: probe.revision,
					});
				})
				.catch(() => {
					if (seq !== loadSeq) return;
					imageState = 'error';
					signal({ status: 'error' });
				});
		});
	});

	function onLoaded() {
		imageState = 'ready';
		onSignal?.({
			status: 'ready', artifactId: resolvedArtifactId, revision, instanceId: target.instanceId,
		});
	}
	function onFailed() {
		imageState = 'error';
		onSignal?.({ status: 'error', instanceId: target.instanceId });
	}
</script>

<!-- The canvas ends above the review bar's floating capsule on a phone, so the capsule never covers its edge or focus ring. -->
<div class="image-viewer" style:--capsule-clearance="{capsuleClearance}px">
	<ReviewBar {artifactId} {draftOpen} bind:capsuleClearance />
	{#if stale && imageState === 'ready'}
		<div class="stale-row" role="status">
			<button type="button" class="stale-control" onclick={() => onReload?.()}>File changed · Reload</button>
		</div>
	{/if}
	{#if imageState === 'loading' && !src}
		<div class="loadbar"><span></span></div>
	{:else if imageState === 'error'}
		<div class="load-error" role="alert">
			<p>Couldn't load this image.</p>
			<button type="button" class="stale-control" onclick={reload}>Try again</button>
		</div>
	{/if}
	{#if src && imageState !== 'error'}
		<ImageCommentLightbox bind:this={lightbox} {src} alt={target.name} {artifactId} {feedback} projectId={target.projectId ?? null} bind:draftOpen onLoaded={onLoaded} onFailed={onFailed} onRetry={reload} />
	{/if}
</div>

<style>
	.image-viewer { --doc-rail: 0px; position: relative; height: 100%; min-height: 0; display: flex; flex-direction: column; gap: 8px; box-sizing: border-box; padding-bottom: var(--capsule-clearance, 0px); }
	.stale-row { display: flex; }
	.stale-control {
		display: inline-flex; align-items: center; height: 28px; padding: 0 8px; border: 0;
		border-radius: var(--radius-sm); background: var(--amber-bg); color: var(--amber-text);
		font-family: var(--font-sans); font-size: var(--text-footnote); font-weight: var(--fw-medium); cursor: pointer;
	}
	.stale-control:hover { background: color-mix(in srgb, var(--amber) 18%, transparent); }
	.stale-control:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	@media (max-width: 768px) { .stale-control { min-height: 44px; padding: 0 12px; } }
</style>
