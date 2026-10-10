<script lang="ts">
	// Renders a workspace image file inside the artifact leaf. Read-only: no
	// editor, comments or draft. The bytes come from the authenticated file
	// route and are only ever shown through <img>, so an SVG can never run
	// script or reach the DOM.
	import './DocViewer.css';
	import { untrack } from 'svelte';
	import { fetchRevision } from '$lib/api/docs';
	import { imageContentUrl } from '$lib/artifactImage';
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

<div class="image-viewer">
	{#if stale && imageState === 'ready'}
		<div class="stale-row" role="status">
			<button type="button" class="stale-control" onclick={() => onReload?.()}>File changed · Reload</button>
		</div>
	{/if}
	{#if imageState === 'loading'}
		<div class="loadbar"><span></span></div>
	{:else if imageState === 'error'}
		<div class="load-error" role="alert">
			<p>Couldn't load this image.</p>
			<button type="button" class="stale-control" onclick={reload}>Try again</button>
		</div>
	{/if}
	{#if src && imageState !== 'error'}
		<div class="stage" class:pending={imageState !== 'ready'}>
			<img {src} alt={target.name} onload={onLoaded} onerror={onFailed} draggable="false" />
		</div>
	{/if}
</div>

<style>
	.image-viewer { position: relative; height: 100%; min-height: 0; display: flex; flex-direction: column; gap: 8px; box-sizing: border-box; }
	.stage { flex: 1; min-height: 0; min-width: 0; display: flex; align-items: center; justify-content: center; overflow: hidden; }
	.stage.pending { visibility: hidden; position: absolute; }
	/* max-* sizing scales the image down to the canvas and never past its natural size. */
	.stage img { display: block; max-width: 100%; max-height: 100%; width: auto; height: auto; object-fit: contain; border-radius: var(--radius-sm); }
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
