<script lang="ts">
	import { renderMarkdown } from '$lib/markdown';

	let { text }: { text: string } = $props();
	let open = $state(false);
	let clamped = $state(false);
	let reveal: HTMLDivElement;
	let expandedHeight = $state(0);

	function trackClamp(node: HTMLElement) {
		const measure = () => {
			if (!reveal) return;
			if (open) expandedHeight = node.scrollHeight;
			else clamped = node.scrollHeight > reveal.clientHeight;
		};
		if (typeof ResizeObserver === 'undefined') {
			requestAnimationFrame(measure);
			return { update: measure, destroy: () => {} };
		}
		const observer = new ResizeObserver(measure);
		observer.observe(node);
		requestAnimationFrame(measure);
		return { update: measure, destroy: () => observer.disconnect() };
	}

	function toggle() {
		if (!open) expandedHeight = reveal.scrollHeight;
		open = !open;
	}
</script>

<div bind:this={reveal} class:open class:clamped={clamped && !open} class="ct-markdown-reveal" style:--ct-expanded-height="{expandedHeight}px">
	<div class="ct-markdown md-content" use:trackClamp>{@html renderMarkdown(text)}</div>
</div>
{#if clamped}<button class="ct-more" onclick={(e) => { e.stopPropagation(); toggle(); }}>{open ? 'Show less' : 'Show more'}</button>{/if}

<style>
	.ct-markdown-reveal { --ct-expanded-height: 4lh; max-height: 4lh; overflow: hidden; transition: max-height var(--dur-standard) var(--ease-entrance); }
	.ct-markdown-reveal.clamped { -webkit-mask-image: linear-gradient(to bottom, black calc(100% - 1lh), transparent); mask-image: linear-gradient(to bottom, black calc(100% - 1lh), transparent); }
	.ct-markdown-reveal.open { max-height: var(--ct-expanded-height); }
	.ct-markdown { font-size: var(--text-subhead); line-height: var(--lh-snug); color: var(--text-primary); }
	.ct-markdown :global(:is(h1, h2, h3, h4, h5, h6)) { font: inherit; font-weight: var(--fw-semibold); margin: 0; }
	.ct-markdown :global(p:first-child) { margin-top: 0; }
	.ct-markdown :global(p:last-child) { margin-bottom: 0; }
	.ct-more { align-self: flex-start; min-height: var(--space-6); margin-top: calc(var(--space-1) * -1); padding: 0; border: 0; background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-caption); font-weight: var(--fw-medium); cursor: pointer; }
	.ct-more:hover { text-decoration: underline; }
	@media (prefers-reduced-motion: reduce) { .ct-markdown-reveal { transition: none; } }
</style>
