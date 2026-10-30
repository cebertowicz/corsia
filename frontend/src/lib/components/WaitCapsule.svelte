<script lang="ts">
	import ThreadWaitingMark from './ThreadWaitingMark.svelte';
	import WaitFaceText from './WaitFaceText.svelte';
	import type { WaitFace } from './waitFace';

	// The chat's wait chip (EW8). With `toggle` it is the run's fold toggle, otherwise a static capsule with no
	// toggle role, chevron or expand name. The toggle is a button laid under the capsule's content, so the label
	// link is its own button beside it, never inside it.
	let { face, threadId = null, toggle = null, element = $bindable() }: {
		face: WaitFace;
		threadId?: string | null;
		toggle?: { expanded: boolean; controls: string | undefined; name: string; onToggle: () => void } | null;
		element?: HTMLElement;
	} = $props();
</script>

<div class="fold-toggle wait-capsule" class:expanded={toggle?.expanded} title={face.title} bind:this={element}>
	{#if toggle}
		<button
			type="button"
			class="capsule-hit"
			aria-expanded={toggle.expanded}
			aria-controls={toggle.controls}
			aria-label={toggle.name}
			onclick={toggle.onToggle}
		></button>
	{/if}
	<span class="capsule-content" class:passive={toggle !== null}>
		<ThreadWaitingMark size={12} />
		<WaitFaceText {face} {threadId} decorative={toggle !== null} />
		{#if toggle}
			<svg class="fold-chevron" class:expanded={toggle.expanded} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
		{/if}
	</span>
</div>

<style>
	/* The same glass capsule as the run's toggle. */
	.wait-capsule {
		position: sticky; top: var(--sticky-top, 0px); z-index: 4;
		display: flex; width: fit-content; max-width: 100%; min-width: 0; min-height: var(--space-7); align-items: center;
		padding: 0 var(--button-pad-x-sm) 0 var(--space-3); border-radius: var(--radius-pill);
		background: var(--glass-thin); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
		box-shadow: var(--glass-rim);
		color: var(--text-secondary); font-size: var(--text-footnote); text-align: left;
	}
	.wait-capsule:not(.expanded) { outline: 1px solid var(--border-subtle); }
	.wait-capsule.expanded { background: var(--glass-regular); box-shadow: var(--glass-elevation-1); }
	.wait-capsule:has(.capsule-hit):hover { color: var(--text-primary); }
	.capsule-hit {
		position: absolute; inset: 0; width: 100%; border: 0; border-radius: inherit; background: transparent; cursor: pointer;
	}
	.capsule-hit:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.capsule-content { display: flex; min-width: 0; align-items: center; gap: var(--space-2); }
	/* A tap anywhere but the link reaches the toggle beneath. */
	.capsule-content.passive { pointer-events: none; }
	.capsule-content.passive :global(.wait-link) { position: relative; z-index: 1; pointer-events: auto; }
	.fold-chevron { flex: 0 0 auto; color: var(--text-tertiary); transition: transform var(--dur-tight); }
	.fold-chevron.expanded { transform: rotate(90deg); }
	@media (pointer: coarse) {
		.wait-capsule { min-height: var(--touch-target); }
	}
</style>
