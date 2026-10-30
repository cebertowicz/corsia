<script lang="ts">
	import { onMount } from 'svelte';
	import { createLivingLogo } from '$lib/livingLogo.js';

	interface Props {
		/** 'play' lands the dots in; 'none' shows the mark at rest (sign-in, where only the colour flow and the pulse run). */
		entry?: 'play' | 'none';
		/** The accessible name; a decorative mark passes none. */
		label?: string;
	}
	const { entry = 'play', label }: Props = $props();
	let host: HTMLDivElement;
	let logo: ReturnType<typeof createLivingLogo> | undefined;

	/** Plays the exit and resolves when the last dot has left, at once for the still mark. Call once, as setup hands over to the first chat. */
	export function exit(): Promise<void> {
		return logo ? logo.exit() : Promise.resolve();
	}

	onMount(() => {
		// The entry waits out the screen's own fade-rise (--dur-standard) so the first big frame is not under a half-faded scene.
		const wait = Number.parseFloat(getComputedStyle(host).getPropertyValue('--dur-standard')) || 0;
		logo = createLivingLogo(host, { entry, delay: entry === 'play' ? wait : 0 });
		return () => { logo?.destroy(); logo = undefined; };
	});
</script>

<div class="living-mark" bind:this={host} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : 'true'}></div>

<style>
	.living-mark { width: 100%; height: 100%; pointer-events: none; user-select: none; }
</style>
