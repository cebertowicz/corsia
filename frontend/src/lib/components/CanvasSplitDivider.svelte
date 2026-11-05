<script lang="ts">
	import {
		dragArtifactWidth, keyboardArtifactWidth, ARTIFACT_MIN, CHAT_MIN, DIVIDER_WIDTH,
	} from '$lib/artifactSplitLayout';

	let { artifactWidth, canvasWidth, onResize }: {
		artifactWidth: number;
		canvasWidth: number;
		onResize: (next: number) => void;
	} = $props();

	let dragging = $state(false);
	let startX = 0;
	let startWidth = 0;

	function onPointerDown(e: PointerEvent) {
		dragging = true;
		startX = e.clientX;
		startWidth = artifactWidth;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}
	function onPointerMove(e: PointerEvent) {
		if (!dragging) return;
		onResize(dragArtifactWidth(startWidth, e.clientX - startX, canvasWidth));
	}
	function onPointerUp() {
		dragging = false;
	}
	function onKeydown(e: KeyboardEvent) {
		const next = keyboardArtifactWidth(artifactWidth, e.key, canvasWidth);
		if (next === null) return;
		e.preventDefault();
		onResize(next);
	}
</script>

<!-- WAI-ARIA Window Splitter pattern: role="separator" + tabindex + arrow-key
     resize is the correct accessible shape here, not a static-element misuse. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	class="divider"
	class:dragging
	role="separator"
	aria-orientation="vertical"
	aria-label="Resize Files panel"
	aria-valuemin={ARTIFACT_MIN}
	aria-valuemax={Math.max(ARTIFACT_MIN, canvasWidth - CHAT_MIN - DIVIDER_WIDTH)}
	aria-valuenow={Math.round(artifactWidth)}
	tabindex="0"
	onpointerdown={onPointerDown}
	onpointermove={onPointerMove}
	onpointerup={onPointerUp}
	onpointercancel={onPointerUp}
	onkeydown={onKeydown}
></div>

<style>
	.divider {
		flex-shrink: 0;
		width: 8px;
		margin: 0;
		position: relative;
		cursor: col-resize;
		touch-action: none;
		z-index: 1;
	}
	.divider::after {
		content: '';
		position: absolute;
		top: 0;
		bottom: 0;
		left: 50%;
		width: 1px;
		background: var(--border);
		transform: translateX(-50%);
		transition: background var(--dur-tight);
	}
	.divider:hover::after,
	.divider.dragging::after,
	.divider:focus-visible::after {
		background: var(--accent);
	}
	.divider:focus-visible {
		outline: none;
	}
	.divider:focus-visible::after,
	.divider.dragging::after {
		width: 2px;
		box-shadow: 0 0 0 2px var(--accent-wash);
	}
</style>
