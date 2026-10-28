<script lang="ts">
	/**
	 * The chat attention stack's live-edge control. On mobile it shares the
	 * action rail's height, material, elevation and radius; on desktop it keeps
	 * the compact size used below the attention dock.
	 */
	let {
		threadId,
		userIsNearBottom,
		onScrollToBottom,
	}: {
		threadId: string | null;
		userIsNearBottom: boolean;
		onScrollToBottom: () => void;
	} = $props();

	const visible = $derived(!userIsNearBottom && !!threadId);
</script>

{#if visible}
	<button
		class="rail-control mobile-scroll-fab press-tint"
		type="button"
		onclick={onScrollToBottom}
		aria-label="Scroll to bottom"
		title="Scroll to bottom"
	>
		<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12l7 7 7-7" /></svg>
	</button>
{/if}

<style>
	.mobile-scroll-fab {
		display: inline-flex; width: var(--touch-target); height: var(--touch-target); padding: 0;
		align-items: center; justify-content: center;
		border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
		border-radius: var(--radius-pill);
		background: var(--glass-regular-thick);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-elevation-1), var(--glass-rim);
		color: var(--text-secondary);
		cursor: pointer;
	}
	@media (min-width: 769px) {
		.mobile-scroll-fab { width: calc(var(--space-7) + var(--space-1)); height: calc(var(--space-7) + var(--space-1)); }
	}
	.mobile-scroll-fab svg {
		width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2;
		stroke-linecap: round; stroke-linejoin: round;
	}
	.mobile-scroll-fab:hover { color: var(--text-primary); }
	.mobile-scroll-fab:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
</style>
