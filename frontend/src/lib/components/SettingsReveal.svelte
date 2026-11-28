<script lang="ts">
	import type { Snippet } from 'svelte';

	/**
	 * A line that arrives under a Settings row without the rows below jumping: its height grows from nothing
	 * (`grid-template-rows`, motion.md "A row arrives, leaves or moves"), then its content fades in. Closed, it takes no
	 * space and cannot be focused.
	 */
	let { open, children }: { open: boolean; children: Snippet } = $props();
</script>

<div class="reveal" class:open>
	<div class="inner" inert={!open}>{@render children()}</div>
</div>

<style>
	/* Closed it also gives back the gap the group puts between its rows, so the rows read the same with or without it. */
	.reveal { display: grid; grid-template-rows: 0fr; margin-top: calc(var(--space-1) * -1); transition: grid-template-rows var(--dur-standard) var(--ease-entrance), margin-top var(--dur-standard) var(--ease-entrance); }
	.reveal.open { grid-template-rows: 1fr; margin-top: 0; }
	/* Clipped while it grows or shrinks; open, a focus ring and a touch target reach past the line. */
	.inner { min-height: 0; overflow: hidden; opacity: 0; transition: opacity var(--dur-tight) var(--ease-snap); }
	.open .inner { overflow: visible; opacity: 1; transition: opacity var(--dur-tight) var(--ease-snap) var(--dur-standard); }
	/* Reduced motion: no growing, a crossfade only. */
	@media (prefers-reduced-motion: reduce) {
		.reveal { transition: none; }
		.open .inner { transition: opacity var(--dur-tight) var(--ease-snap); }
	}
</style>
