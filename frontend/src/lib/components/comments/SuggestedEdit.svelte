<script lang="ts">
	import { lineDiff } from '$lib/diff';
	import ToolDiff from '$lib/components/ToolDiff.svelte';

	/** A suggestion's edit: a long one shows its first lines and says there is more; the card never scrolls it on its own. */
	let { before, after }: { before: string; after: string } = $props();
	const lines = $derived(lineDiff(before, after));
	let open = $state(false);
	let clamped = $state(false);
	function track(node: HTMLElement) {
		const measure = () => { if (!open) clamped = node.scrollHeight > node.clientHeight + 1; };
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
		observer?.observe(node);
		if (node.firstElementChild) observer?.observe(node.firstElementChild);
		requestAnimationFrame(measure);
		return { destroy: () => observer?.disconnect() };
	}
</script>

{#if lines}
	<div class="ct-diff" class:clamped={clamped && !open} class:open use:track><ToolDiff file="Suggested edit" {lines} words fill /></div>
	{#if clamped}<button class="ct-show-all" onclick={(e) => { e.stopPropagation(); open = !open; }}>{open ? 'Show less' : 'Show all'}</button>{/if}
{/if}

<style>
	/* Its first lines, with the rest faded and Show all under it, like a long comment's Show more. */
	.ct-diff { flex-shrink: 0; max-height: 10lh; overflow: hidden; }
	.ct-diff.clamped { mask-image: linear-gradient(to bottom, black calc(100% - 2lh), transparent); }
	.ct-diff.open { max-height: none; }
	.ct-show-all { align-self: flex-start; min-height: var(--space-6); padding: 0; border: 0; background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-caption); font-weight: var(--fw-medium); cursor: pointer; }
	.ct-show-all:hover { text-decoration: underline; }
	@media (pointer: coarse) { .ct-show-all { min-height: var(--touch-target); } }
</style>
