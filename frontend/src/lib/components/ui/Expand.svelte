<script lang="ts">
	import { tick, untrack, type Snippet } from 'svelte';

	/**
	 * The one way a region opens and closes: it grows from its header (height through
	 * `grid-template-rows` 0fr → 1fr, content fading in) and stays mounted while it
	 * closes so the exit is seen. Closed and finished closing, it renders nothing. A region
	 * that is open on first render does not animate.
	 */
	let {
		open,
		present = $bindable(open),
		keep = false,
		id,
		children,
	}: {
		open: boolean;
		/** True while the content is mounted: open, or still closing. */
		present?: boolean;
		/** Keep the content rendered while closed (hide it with `present`), for markup other code reads. */
		keep?: boolean;
		id?: string;
		children: Snippet;
	} = $props();

	const OPEN_MS = 220;
	const CLOSE_MS = 154;
	/** Reduced motion trades the size change for a short crossfade. */
	const REDUCED_MS = 120;

	let shown = $state(untrack(() => open));
	let settled = $state(untrack(() => open));
	let element: HTMLElement | undefined = $state();
	let timer: ReturnType<typeof setTimeout> | undefined;
	let generation = 0;

	/** The transition's own end finishes the change; the timer only covers one that never reports it. */
	const SLACK_MS = 80;

	function duration(ms: number): number {
		return (typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches ? REDUCED_MS : ms) + SLACK_MS;
	}

	function finish(): void {
		clearTimeout(timer);
		if (shown) settled = true;
		else present = false;
	}

	$effect(() => {
		const wantOpen = open;
		untrack(() => {
			clearTimeout(timer);
			const mine = ++generation;
			if (wantOpen && shown && present) return;
			if (!wantOpen && !shown && !present) return;
			if (wantOpen) {
				settled = false;
				present = true;
				void tick().then(() => {
					if (mine !== generation) return;
					void element?.offsetHeight;
					shown = true;
					timer = setTimeout(finish, duration(OPEN_MS));
				});
			} else {
				settled = false;
				shown = false;
				timer = setTimeout(finish, duration(CLOSE_MS));
			}
		});
		return () => clearTimeout(timer);
	});
</script>

{#if keep || open || present}
	<div class="expand" class:open={shown} class:settled bind:this={element} inert={!open} {id}>
		<div class="expand-inner" ontransitionend={(event) => { if (event.target === event.currentTarget && event.propertyName === 'opacity') finish(); }}>{@render children()}</div>
	</div>
{/if}

<style>
	.expand {
		display: grid; grid-template-rows: 0fr;
		/* An exit is about 70% of its entrance. */
		transition: grid-template-rows calc(var(--dur-standard) * 0.7) var(--ease-snap);
	}
	.expand.open {
		grid-template-rows: 1fr;
		transition-duration: var(--dur-standard);
		transition-timing-function: var(--ease-entrance);
	}
	.expand-inner {
		min-height: 0; overflow: hidden; opacity: 0;
		transition: opacity calc(var(--dur-standard) * 0.7) var(--ease-snap);
	}
	.expand.open > .expand-inner { opacity: 1; transition-duration: var(--dur-standard); transition-timing-function: var(--ease-entrance); }
	/* Once open, let focus rings and shadows paint past the box. */
	.expand.settled > .expand-inner { overflow: visible; }
	@media (prefers-reduced-motion: reduce) {
		.expand, .expand.open { transition: none; }
		/* The global reduced-motion rule zeroes every duration; the crossfade is the one motion kept. */
		.expand-inner, .expand.open > .expand-inner { transition: opacity var(--dur-tight) var(--ease-snap) !important; }
	}
</style>
