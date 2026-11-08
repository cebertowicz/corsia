<script lang="ts">
	import { get } from 'svelte/store';
	import { fade, fly } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { ms } from '$lib/popoverMotion';
	import { prefersReducedMotion } from '$lib/stores/viewport';

	// The document's one jump control, in the bottom band's slot: the same control chat's transcript uses, counting this
	// document's unread comment answers and suggestions together. What it points at is read in the band, which owns the row.
	let { count, direction, label, onJump }: {
		count: number;
		direction: 'above' | 'below';
		/** The thread's name in full, for a screen reader and the hover tip. */
		label: string;
		onJump: () => void;
	} = $props();

	// Arrives where it lands (fade, 4px rise) and leaves by fading at 70%; reduced motion crossfades only (motion.md).
	function arrive(node: HTMLElement) {
		const reduced = get(prefersReducedMotion);
		return fly(node, { y: reduced ? 0 : 4, duration: ms(reduced ? '--dur-tight' : '--dur-standard'), easing: cubicOut });
	}
	function leave(node: HTMLElement) {
		return fade(node, { duration: ms('--dur-standard') * 0.7 });
	}
</script>

<div class="doc-jump" data-doc-jump in:arrive|global out:leave|global>
	<button class="doc-jump-control press-tint" type="button" onclick={onJump} aria-label={label} title={label}>
		<span aria-hidden="true">{direction === 'above' ? '↑' : '↓'}</span><span>{count} new</span>
	</button>
</div>

<style>
	/* The image host draws its region layer over the whole surface (`ImageRegionLayer`: position absolute, inset 0, z-index 1)
	 * so that a stroke can start anywhere on the picture; the control has to answer a tap above it. */
	.doc-jump { position: relative; z-index: 2; display: flex; align-items: center; min-width: 0; min-height: var(--touch-target); max-width: 100%; }
	/* The same control chat's transcript uses: the same tokens, shape and size (ChatScrollControl). */
	.doc-jump-control {
		display: inline-flex; width: auto; height: var(--touch-target); padding: 0 var(--space-3);
		align-items: center; justify-content: center; gap: var(--space-1);
		border: 1px solid color-mix(in srgb, var(--border) 72%, transparent); border-radius: var(--radius-pill);
		background: var(--glass-regular-thick); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-elevation-1), var(--glass-rim); color: var(--text-secondary); cursor: pointer; font: inherit;
	}
	/* Chat's control is 36px wide on a pointer-fine desktop; the same breakpoint, written in rem as
		ConnectedAppsSettings does, because the token ratchet takes no raw px in a new file. */
	@media (min-width: 48.0625rem) { .doc-jump-control { height: calc(var(--space-7) + var(--space-1)); } }
	.doc-jump-control:hover { color: var(--text-primary); }
	.doc-jump-control:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
</style>
