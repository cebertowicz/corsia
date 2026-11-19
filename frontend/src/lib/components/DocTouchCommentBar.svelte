<script lang="ts">
	let { column, offered, onComment }: { column: HTMLElement | undefined; offered: boolean; onComment: () => void } = $props();
	let bar = $state<HTMLDivElement>();
	let bottom = $state(0);
	let left = $state(0);
	let width = $state(0);
	$effect(() => {
		const root = column, element = bar;
		if (!root || !element) return;
		const viewport = window.visualViewport;
		const update = () => {
			const bounds = root.getBoundingClientRect();
			left = bounds.left;
			width = bounds.width;
			bottom = Math.max(0, window.innerHeight - ((viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight)));
			root.style.setProperty('--document-comment-room', `${element.getBoundingClientRect().height}px`);
		};
		update();
		const observer = new ResizeObserver(update);
		observer.observe(element);
		observer.observe(root);
		viewport?.addEventListener('resize', update);
		viewport?.addEventListener('scroll', update);
		window.addEventListener('resize', update);
		window.addEventListener('scroll', update, true);
		return () => {
			observer.disconnect();
			viewport?.removeEventListener('resize', update);
			viewport?.removeEventListener('scroll', update);
			window.removeEventListener('resize', update);
			window.removeEventListener('scroll', update, true);
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
