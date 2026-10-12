<script lang="ts">
	// The comments drawer's row for one image (DCOL-FR13.6): the whole image at
	// most 72 px tall, with the listed comments' boxes and points drawn and
	// numbered, so the numbers below it can be found on it.
	import { onMount } from 'svelte';
	import type { DocComment } from '$lib/stores/docComments';
	import { isPoint, placeTags, type ImageRegion } from '$lib/imageRegions';

	type Pinned = DocComment & { region: ImageRegion; regionNumber: number };
	let { src, alt = '', caption = false, comments, colorOf, onOpen }: {
		src: string;
		alt?: string;
		/** Name the image beside it by its alt text, where the image itself is not in view. */
		caption?: boolean;
		comments: DocComment[];
		colorOf: (comment: DocComment) => string;
		/** Show the image itself, at Fit. */
		onOpen?: () => void;
	} = $props();

	let frame = $state<HTMLElement>();
	let width = $state(0);
	let height = $state(0);
	const pinned = $derived(comments.filter((comment): comment is Pinned => !!comment.region && comment.regionNumber != null).sort((a, b) => a.regionNumber - b.regionNumber));
	const marks = $derived.by(() => {
		if (!width || !height) return [];
		const positions = placeTags(pinned.map((comment) => comment.region), { width, height }, 7, 2);
		return pinned.map((comment, index) => ({ comment, ...positions[index] }));
	});
	function measure() {
		const rect = frame?.getBoundingClientRect();
		if (rect) { width = rect.width; height = rect.height; }
	}
	onMount(() => {
		const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
		if (frame) observer?.observe(frame);
		measure();
		return () => observer?.disconnect();
	});
	const pct = (value: number) => `${value * 100}%`;
	const numbers = $derived(pinned.map((comment) => comment.regionNumber).join(', '));
</script>

<button type="button" class="thumb" data-region-thumb onclick={() => onOpen?.()} aria-label={`${alt ? `${alt}: ` : ''}image with comment${pinned.length === 1 ? '' : 's'} ${numbers}`} disabled={!onOpen}>
	<span class="frame" bind:this={frame}>
		<img {src} alt="" draggable="false" onload={measure} />
		{#each pinned as comment (comment.id)}
			{#if isPoint(comment.region)}
				<span class="dot" style:left={pct(comment.region.x)} style:top={pct(comment.region.y)} style:--region-color={colorOf(comment)}></span>
			{:else}
				<span class="box" class:stale={comment.regionStale} style:left={pct(comment.region.x)} style:top={pct(comment.region.y)} style:width={pct(comment.region.w)} style:height={pct(comment.region.h)} style:--region-color={colorOf(comment)}></span>
			{/if}
		{/each}
		{#each marks as { comment, x, y } (comment.id)}
			<span class="num" class:stale={comment.regionStale} style:left="{x}px" style:top="{y}px" style:--region-color={colorOf(comment)} data-thumb-number={comment.regionNumber}>{comment.regionNumber}</span>
		{/each}
	</span>
	{#if caption && alt}<span class="caption" data-thumb-caption aria-hidden="true">{alt}</span>{/if}
</button>

<style>
	.thumb { display: flex; align-items: flex-start; justify-content: flex-start; gap: var(--space-2); width: 100%; padding: 2px 0 6px; border: 0; background: none; color: inherit; font: inherit; text-align: start; cursor: pointer; }
	.caption {
		flex: 1 1 0; min-width: 4em; overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; line-clamp: 3;
		color: var(--text-secondary); font-size: var(--text-caption); line-height: 1.35; overflow-wrap: anywhere;
	}
	.thumb:disabled { cursor: default; }
	.thumb:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.frame { position: relative; display: inline-block; flex: 0 1 auto; min-width: 0; max-width: 100%; line-height: 0; border-radius: var(--radius-2xs); box-shadow: 0 0 0 1px var(--border); }
	img { display: block; max-width: 100%; max-height: 72px; width: auto; height: auto; border-radius: inherit; user-select: none; }
	.box { position: absolute; box-sizing: border-box; border: 1.5px solid var(--region-color); box-shadow: 0 0 0 1px var(--image-region-halo); pointer-events: none; }
	.box.stale { border-style: dashed; }
	.dot { position: absolute; width: 6px; height: 6px; margin: -3px 0 0 -3px; border-radius: 50%; background: var(--region-color); box-shadow: 0 0 0 1px var(--image-region-halo); }
	.num {
		position: absolute; box-sizing: border-box; display: grid; place-items: center; width: 14px; height: 14px; margin: -7px 0 0 -7px; border-radius: 50%;
		background: var(--region-color); color: var(--image-region-ink); border: 1px solid color-mix(in srgb, var(--region-color) 70%, var(--image-region-edge)); box-shadow: 0 0 0 1px var(--image-region-halo);
		font-size: var(--text-caption); font-weight: var(--fw-bold); line-height: 1; font-variant-numeric: tabular-nums;
	}
	.num.stale { border-style: dashed; }
</style>
