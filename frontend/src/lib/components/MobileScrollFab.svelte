<script lang="ts">
	/**
	 * One peer of the mobile action rail. Height, material, elevation and radius
	 * are the rail contract (see `ChatAttentionStack`): this live-edge control is
	 * distinguished by being present only while the user is scrolled up, never by
	 * being smaller or flatter than its neighbours.
	 */
	import { isMobile } from '$lib/stores/viewport';

	let {
		threadId,
		userIsNearBottom,
		onScrollToBottom,
	}: {
		threadId: string | null;
		userIsNearBottom: boolean;
		onScrollToBottom: () => void;
	} = $props();

	const visible = $derived($isMobile && !userIsNearBottom && !!threadId);
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
		display: inline-flex; width: 44px; height: 44px; padding: 0;
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
	.mobile-scroll-fab svg {
		width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2;
		stroke-linecap: round; stroke-linejoin: round;
	}
	.mobile-scroll-fab:hover { color: var(--text-primary); }
	.mobile-scroll-fab:focus-visible { outline: 2px solid var(--violet); outline-offset: 3px; }
</style>
