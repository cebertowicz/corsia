<script lang="ts">
	import IconButton from './IconButton.svelte';

	/**
	 * The one Close for a sheet's top right: a bare X, named "Close", 44 px on a touch screen, with the accent focus ring the
	 * rest of the comment surfaces draw. The comment list, a card or thread sheet, a chat comment sheet and the image sheet
	 * all use it, so the same job is drawn, named and focused the same way.
	 */
	let { onclick, label = 'Close' }: { onclick: (e: MouseEvent) => void; label?: string } = $props();
</script>

<IconButton class="close-button" ariaLabel={label} variant="ghost" size="sm" {onclick}>
	<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
</IconButton>

<style>
	/* `IconButton` transitions `all`, which would animate the focus ring's width and colour as it lands; the ring is drawn at once. */
	:global(.ico-btn.close-button) { transition-property: background-color, color, opacity, box-shadow; }
	:global(.ico-btn.close-button:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* Three classes, so `IconButton`'s `.ico-btn.sm` (24px) loses whatever order the two stylesheets load in. */
	@media (pointer: coarse), (max-width: 48em) { :global(.ico-btn.sm.close-button) { width: var(--touch-target); height: var(--touch-target); } }
</style>
