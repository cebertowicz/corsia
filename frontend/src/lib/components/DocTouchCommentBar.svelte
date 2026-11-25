<script lang="ts">
	let { column, offered, onComment }: { column: HTMLElement | undefined; offered: boolean; onComment: () => void } = $props();
	let bar = $state<HTMLDivElement>();
	let bottom = $state(0);
	let left = $state(0);
	let width = $state(0);
	$effect(() => {
		const root = column, element = bar;
		// Measured again each time the offer comes or goes: the column can move (a page or panel sliding in) without resizing, and the bar must not keep an earlier place.
		void offered;
		if (!root || !element) return;
		const viewport = window.visualViewport;
		const update = () => {
			const bounds = root.getBoundingClientRect();
			left = bounds.left;
			width = bounds.width;
			const lift = Math.max(0, window.innerHeight - ((viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight)));
			bottom = lift;
			// The room is what the bar covers of the document's column, never more: a bar on a column that ends above the page's foot covers less than its own height.
			// The bar's top is worked out from the lift just set, not read back: the lift reaches the page at the next flush, so a measured top is the one before the move.
			const height = element.getBoundingClientRect().height;
			root.style.setProperty('--document-comment-room', `${Math.max(0, Math.min(height, bounds.bottom - (window.innerHeight - lift - height)))}px`);
		};
		update();
		const observer = new ResizeObserver(update);
		observer.observe(element);
		observer.observe(root);
		viewport?.addEventListener('resize', update);
		viewport?.addEventListener('scroll', update);
		window.addEventListener('resize', update);
		window.addEventListener('scroll', update, true);
		// A column sliding into place moves without a resize or a scroll: its transition or animation ending is the moment it has arrived.
		window.addEventListener('transitionend', update, true);
		window.addEventListener('animationend', update, true);
		return () => {
			observer.disconnect();
			viewport?.removeEventListener('resize', update);
			viewport?.removeEventListener('scroll', update);
			window.removeEventListener('resize', update);
			window.removeEventListener('scroll', update, true);
			window.removeEventListener('transitionend', update, true);
			window.removeEventListener('animationend', update, true);
			root.style.removeProperty('--document-comment-room');
		};
	});
</script>

<div class="touch-comment-bar" class:offered bind:this={bar} style:bottom="{bottom}px" style:left="{left}px" style:width="{width}px" aria-hidden={!offered}>
	{#if offered}<button type="button" class="sel-comment" onpointerdown={(event) => event.preventDefault()} onmousedown={(event) => event.preventDefault()} onclick={onComment}>Comment</button>{/if}
</div>

<style>
	.touch-comment-bar { position: fixed; z-index: 5; display: flex; justify-content: flex-end; box-sizing: border-box; min-height: calc(var(--touch-target) + var(--space-2) + max(var(--space-2), env(safe-area-inset-bottom))); padding: var(--space-2); padding-bottom: max(var(--space-2), env(safe-area-inset-bottom)); background: var(--bg-elevated); border-top: 1px solid var(--border-subtle); visibility: hidden; pointer-events: none; }
	.touch-comment-bar.offered { visibility: visible; pointer-events: auto; }
	button.sel-comment { position: static; translate: none; animation: none; box-shadow: none; min-width: var(--touch-target); min-height: var(--touch-target); padding-inline: var(--space-4); color: var(--accent-text); font: inherit; font-weight: var(--fw-semibold); border: 0; border-radius: var(--radius); background: var(--bg-hover); cursor: pointer; }
</style>
