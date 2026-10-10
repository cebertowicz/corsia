<script lang="ts">
	import { paneOpen, paneDragging } from '$lib/stores/paneOpen';
	import { paneCollapsed } from '$lib/stores/paneCollapsed';
	import { uiScope } from '$lib/stores/uiScope';
	import { PANE_DRAWER_WIDTH, shouldOverlayPane } from '$lib/artifactSplitLayout';

	let { label, protectArtifactSplit = false, children }: {
		label: string;
		/** Only a visible artifact leaf may make the desktop drawer overlay the canvas. */
		protectArtifactSplit?: boolean;
		children: import('svelte').Snippet;
	} = $props();

	let paneEl = $state<HTMLElement | undefined>(undefined);
	let overlay = $state(false);

	$effect(() => {
		const pane = paneEl;
		const main = pane?.previousElementSibling;
		if (!(main instanceof HTMLElement) || typeof window === 'undefined') return;

		const updateOverlay = () => {
			const measuredMainWidth = main.getBoundingClientRect().width;
			const inlineCanvasWidth = overlay
				? measuredMainWidth - PANE_DRAWER_WIDTH
				: measuredMainWidth;
			overlay = !$paneCollapsed && shouldOverlayPane(
				protectArtifactSplit, window.innerWidth, inlineCanvasWidth,
			);
		};
		updateOverlay();

		if (typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(updateOverlay);
		observer.observe(main);
		window.addEventListener('resize', updateOverlay);
		return () => {
			observer.disconnect();
			window.removeEventListener('resize', updateOverlay);
		};
	});
</script>

<aside
	bind:this={paneEl}
	class="pane"
	class:open={$paneOpen}
	class:dragging={$paneDragging}
	class:collapsed={$paneCollapsed}
	class:overlay
	style={`--pane-width: ${PANE_DRAWER_WIDTH}px`}
>
	<div class="pane-top">
		<span class="pane-scope" class:accent={$uiScope != null}>{label}</span>
	</div>
	{@render children()}
</aside>

<style>
	.pane {
		width: var(--pane-width); flex-shrink: 0; height: 100%; overflow-y: auto;
		background: var(--glass-thick);
		backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-rim);
		border-left: 1px solid var(--border-subtle);
		padding: 0 14px 24px;
		display: flex; flex-direction: column; gap: 1px; z-index: 11;
		transition: width var(--dur-spring) var(--ease-material),
		            padding var(--dur-spring) var(--ease-material);
	}
	.pane-top {
		position: sticky; top: 0; z-index: 3; flex-shrink: 0;
		display: flex; align-items: center; justify-content: space-between;
		height: var(--header-height, 56px);
		margin: 0 -14px 8px; padding: 0 12px 0 18px;
		background: var(--glass-thick);
	}
	.pane-scope { font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.pane-scope.accent { color: var(--accent); }

	@media (min-width: 1025px) {
		.pane.overlay {
			position: absolute; top: 0; right: 0; bottom: 0;
		}
		.pane.collapsed {
			width: 0; padding-left: 0; padding-right: 0;
			border-left: none; overflow: hidden;
		}
	}

	@media (max-width: 1024px) {
		.pane {
			display: none;
			position: fixed; top: 0; right: 0; bottom: 0; left: auto;
			width: min(420px, 92vw); height: 100%; max-height: none;
			border-top: none; border-left: none; border-radius: 0;
			z-index: 0; transform: none;
			padding: 0 16px calc(24px + env(safe-area-inset-bottom, 0px));
		}
		.pane.open, .pane.dragging { display: flex; }
		.pane-top {
			height: calc(var(--header-height, 56px) + env(safe-area-inset-top, 0px));
			padding-top: env(safe-area-inset-top, 0px);
		}
	}
</style>
