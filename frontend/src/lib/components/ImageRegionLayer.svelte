<script lang="ts">
	// The numbered tags, boxes and point pins over an image in the viewer
	// (DCOL-FR13.3, FR13.4, FR13.9), and the surface a box or point is drawn on.
	// It fills the viewer's frame, which is the image at its shown size, so
	// regions are placed in fractions and tags keep their size at any zoom.
	import { onMount } from 'svelte';
	import type { DocComment } from '$lib/stores/docComments';
	import {
		describeRegion, isPoint, nearestTarget, placeTag, regionFromDrag, regionRect, type ImageRegion, type Point,
	} from '$lib/imageRegions';

	type Pinned = DocComment & { region: ImageRegion; regionNumber: number };
	let { comments, colorOf, draftColor, draft = null, selectedId = null, hoveredId = null, boxMode = false, cardOpen = false, readOnly = false, scale = 1, onPick, onHover, onDraw, onDismiss }: {
		/** Region comments to show; a resolved one shows only while it is selected. */
		comments: DocComment[];
		colorOf: (comment: DocComment) => string;
		/** The colour a new comment by the person takes. */
		draftColor: string;
		/** The region of the comment being written. */
		draft?: ImageRegion | null;
		selectedId?: string | null;
		hoveredId?: string | null;
		/** The Comment button is pressed: touch draws too, and Enter here comments on the whole image. */
		boxMode?: boolean;
		/** A card or composer is open, so a click on the image only closes it. */
		cardOpen?: boolean;
		readOnly?: boolean;
		/** The viewer's zoom; the tags are placed again when it changes. */
		scale?: number;
		onPick: (id: string, anchor: () => DOMRect | null, opener: HTMLElement) => void;
		onHover: (id: string | null) => void;
		onDraw: (region: ImageRegion, anchor: () => DOMRect | null) => void;
		onDismiss: () => void;
	} = $props();

	let layer = $state<HTMLElement>();
	let surface = $state<HTMLElement>();
	let width = $state(0);
	let height = $state(0);
	let spaceHeld = false;
	let stroke = $state<{ pointer: number; from: Point; to: Point; closes: boolean } | null>(null);
	let focusedTag = $state<string | null>(null);
	// Agent comments that arrive while the viewer is open sweep in; those already there do not.
	let known: Set<string> | null = null;
	let sweeping = $state<Set<string>>(new Set());

	const pinned = $derived(
		comments
			.filter((comment): comment is Pinned => !!comment.region && comment.regionNumber != null && (!comment.resolved || comment.id === selectedId))
			.sort((a, b) => a.regionNumber - b.regionNumber),
	);
	const size = $derived({ width, height });
	const tags = $derived(width && height ? pinned.map((comment) => ({ comment, ...placeTag(comment.region, size) })) : []);
	const preview = $derived(stroke ? regionFromDrag(stroke.from, stroke.to, size) : draft);

	$effect(() => {
		const ids = pinned.map((comment) => comment.id);
		if (known === null) { known = new Set(ids); return; }
		const arrived = pinned.filter((comment) => comment.authorKind === 'agent' && !known!.has(comment.id)).map((comment) => comment.id);
		for (const id of ids) known.add(id);
		if (!arrived.length) return;
		sweeping = new Set([...sweeping, ...arrived]);
		const timer = setTimeout(() => { sweeping = new Set([...sweeping].filter((id) => !arrived.includes(id))); }, 1400);
		return () => clearTimeout(timer);
	});

	function measure() {
		const rect = layer?.getBoundingClientRect();
		if (rect) { width = rect.width; height = rect.height; }
	}
	$effect(() => { void scale; measure(); });
	onMount(() => {
		const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
		if (layer) observer?.observe(layer);
		return () => observer?.disconnect();
	});

	onMount(() => {
		const down = (event: KeyboardEvent) => { if (event.code === 'Space') spaceHeld = true; };
		const up = (event: KeyboardEvent) => { if (event.code === 'Space') spaceHeld = false; };
		const blur = () => { spaceHeld = false; };
		window.addEventListener('keydown', down);
		window.addEventListener('keyup', up);
		window.addEventListener('blur', blur);
		return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); window.removeEventListener('blur', blur); };
	});

	/** Where a region is on screen now, for the card that pops over beside it. */
	export function anchorFor(region: ImageRegion): () => DOMRect | null {
		return () => (layer?.isConnected ? regionRect(region, layer.getBoundingClientRect()) : null);
	}
	/** Moves focus to the drawing surface, where Enter comments on the whole image. */
	export function focusSurface() { surface?.focus({ preventScroll: true }); }

	function local(event: PointerEvent): Point {
		const rect = layer!.getBoundingClientRect();
		return { x: event.clientX - rect.left, y: event.clientY - rect.top };
	}
	function startStroke(event: PointerEvent) {
		if (readOnly || !layer || event.button !== 0) return;
		// A mouse draws at any zoom unless Space is held, which pans; one finger draws only in box mode, otherwise it pans.
		if (event.pointerType === 'mouse' ? spaceHeld : !boxMode) return;
		event.stopPropagation();
		event.preventDefault();
		layer.setPointerCapture?.(event.pointerId);
		const at = local(event);
		stroke = { pointer: event.pointerId, from: at, to: at, closes: cardOpen };
	}
	function moveStroke(event: PointerEvent) {
		if (stroke?.pointer !== event.pointerId) return;
		stroke = { ...stroke, to: local(event) };
	}
	function endStroke(event: PointerEvent) {
		if (stroke?.pointer !== event.pointerId) return;
		const done = { ...stroke, to: local(event) };
		stroke = null;
		if (event.type === 'pointercancel') return;
		const region = regionFromDrag(done.from, done.to, size);
		// A click while a card is open only closes it; a drag always draws.
		if (done.closes && isPoint(region)) { onDismiss(); return; }
		onDraw(region, anchorFor(region));
	}

	function onTagDown(event: PointerEvent) {
		// The tag takes its own press, so the viewer neither pans nor captures the pointer away from it.
		event.stopPropagation();
	}
	function pick(event: MouseEvent, id: string) {
		event.stopPropagation();
		let chosen = id;
		// Overlapping 44 px targets go to the nearer centre; a key press is on the focused tag.
		if (event.detail > 0 && layer) {
			const box = layer.getBoundingClientRect();
			chosen = nearestTarget(tags.map((tag) => ({ id: tag.comment.id, x: box.left + tag.x, y: box.top + tag.y })), { x: event.clientX, y: event.clientY }) ?? id;
		}
		const comment = pinned.find((item) => item.id === chosen);
		if (comment) onPick(chosen, anchorFor(comment.region), event.currentTarget as HTMLElement);
	}
	function onSurfaceKey(event: KeyboardEvent) {
		if (event.key !== 'Enter' || readOnly) return;
		event.preventDefault();
		const whole = { x: 0, y: 0, w: 1, h: 1 };
		onDraw(whole, anchorFor(whole));
	}

	const pct = (value: number) => `${value * 100}%`;
	const label = (comment: Pinned) => `Comment ${comment.regionNumber} by ${comment.authorKind === 'agent' ? comment.author : 'you'}, ${describeRegion(comment.region)}${comment.regionStale ? ', image changed since this comment' : ''}`;
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="regions"
	class:drawing={!readOnly}
	class:box-mode={boxMode}
	data-image-regions
	bind:this={layer}
	onpointerdown={startStroke}
	onpointermove={moveStroke}
	onpointerup={endStroke}
	onpointercancel={endStroke}
>
	{#if boxMode && !readOnly}
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<div class="surface" bind:this={surface} tabindex="0" role="button" aria-label="Comment on the whole image: press Enter, or drag on the image to mark part of it" onkeydown={onSurfaceKey}></div>
	{/if}
	{#each pinned as comment (comment.id)}
		{@const shown = comment.id === selectedId || comment.id === hoveredId || comment.id === focusedTag || sweeping.has(comment.id)}
		{#if isPoint(comment.region)}
			<span class="dot" class:faint={!shown} class:stale={comment.regionStale} style:left={pct(comment.region.x)} style:top={pct(comment.region.y)} style:--region-color={colorOf(comment)} aria-hidden="true"></span>
		{:else if shown}
			<span class="box" class:stale={comment.regionStale} class:sweep={sweeping.has(comment.id)} style:left={pct(comment.region.x)} style:top={pct(comment.region.y)} style:width={pct(comment.region.w)} style:height={pct(comment.region.h)} style:--region-color={colorOf(comment)} aria-hidden="true" data-region-box={comment.id}></span>
		{/if}
	{/each}
	{#if preview}
		{#if isPoint(preview)}
			<span class="dot draft" style:left={pct(preview.x)} style:top={pct(preview.y)} style:--region-color={draftColor} aria-hidden="true"></span>
		{:else}
			<span class="box draft" style:left={pct(preview.x)} style:top={pct(preview.y)} style:width={pct(preview.w)} style:height={pct(preview.h)} style:--region-color={draftColor} aria-hidden="true" data-region-draft></span>
		{/if}
	{/if}
	{#each tags as { comment, x, y, side } (comment.id)}
		{@const agent = comment.authorKind === 'agent'}
		<button
			type="button"
			class="tag"
			class:faint={comment.id !== selectedId && comment.id !== hoveredId}
			class:selected={comment.id === selectedId}
			class:stale={comment.regionStale}
			class:leftward={side === 'left' || side === 'up-left' || side === 'down-left'}
			style:left="{x}px"
			style:top="{y}px"
			style:--region-color={colorOf(comment)}
			aria-label={label(comment)}
			aria-pressed={comment.id === selectedId}
			data-region-tag={comment.regionNumber}
			onpointerdown={onTagDown}
			onclick={(event) => pick(event, comment.id)}
			onpointerenter={() => onHover(comment.id)}
			onpointerleave={() => onHover(null)}
			onfocus={() => { focusedTag = comment.id; onHover(comment.id); }}
			onblur={() => { if (focusedTag === comment.id) focusedTag = null; onHover(null); }}
		><span class="disc">{comment.regionNumber}</span>{#if agent}<span class="name">{comment.author}</span>{/if}</button>
	{/each}
</div>

<style>
	.regions { position: absolute; inset: 0; z-index: 1; }
	.regions.drawing { cursor: crosshair; }
	.surface { position: absolute; inset: 0; border-radius: inherit; }
	.surface:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.regions.box-mode { box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--accent) 55%, transparent); }

	/* A box is the author's colour inside a thin white line, so it reads on light and dark parts of a screenshot. */
	.box { position: absolute; box-sizing: border-box; border: 2px solid var(--region-color); border-radius: 2px; box-shadow: 0 0 0 1px #fff, inset 0 0 0 1px #fff; pointer-events: none; }
	.box.stale { border-style: dashed; opacity: 0.55; }
	.box.draft { border-style: solid; background: color-mix(in srgb, var(--region-color) 10%, transparent); }
	.box.sweep { animation: sweep 600ms cubic-bezier(0.22, 1, 0.36, 1); }
	@keyframes sweep { from { clip-path: inset(0 100% 100% 0); } to { clip-path: inset(0 0 0 0); } }

	/* A point pin is a 14 px dot. */
	.dot { position: absolute; width: 14px; height: 14px; margin: -7px 0 0 -7px; border-radius: 50%; background: var(--region-color); box-shadow: 0 0 0 2px #fff; pointer-events: none; }
	.dot.faint { background: color-mix(in srgb, var(--region-color) 80%, transparent); }
	.dot.stale { opacity: 0.55; outline: 1.5px dashed var(--region-color); outline-offset: 3px; }

	/* The 22 px disc sits on the placed centre; an agent's name grows away from the box it points at. */
	.tag {
		position: absolute; display: flex; align-items: center; gap: 4px; padding: 0; border: 0; background: none;
		transform: translate(-11px, -11px); font: inherit; cursor: pointer; -webkit-tap-highlight-color: transparent;
	}
	.tag.leftward { flex-direction: row-reverse; transform: translate(calc(-100% + 11px), -11px); }
	/* An invisible 44 px target around the disc; overlapping ones go to the nearer centre. */
	.tag::before { content: ''; position: absolute; left: -11px; top: -11px; width: 44px; height: 44px; }
	.tag.leftward::before { left: auto; right: -11px; }
	.disc {
		box-sizing: border-box; display: grid; place-items: center; width: 22px; height: 22px; border-radius: 50%;
		background: var(--region-color); color: #111; border: 1.5px solid color-mix(in srgb, var(--region-color) 70%, #000);
		box-shadow: 0 0 0 1.5px #fff; font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1;
		transition: background var(--dur-tight, 120ms);
	}
	.name {
		max-width: 14ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 2px 6px; border-radius: var(--radius-pill, 999px);
		background: var(--region-color); color: #111; border: 1.5px solid color-mix(in srgb, var(--region-color) 70%, #000); box-shadow: 0 0 0 1.5px #fff;
		font-size: 11px; font-weight: 600; line-height: 14px;
	}
	/* Faint until hovered, focused or picked: only the fill fades; the digit, border and white ring stay solid. */
	.tag.faint .disc, .tag.faint .name { background: color-mix(in srgb, var(--region-color) 80%, transparent); }
	.tag.selected .disc { box-shadow: 0 0 0 1.5px #fff, 0 0 0 4px var(--region-color); }
	.tag.stale { opacity: 0.55; }
	.tag.stale .disc, .tag.stale .name { border-style: dashed; background: color-mix(in srgb, var(--region-color) 70%, transparent); }
	.tag:hover .disc, .tag:hover .name { background: var(--region-color); }
	.tag:focus-visible { outline: none; }
	.tag:focus-visible .disc { background: var(--region-color); outline: 2px solid var(--accent); outline-offset: 3px; }
	@media (prefers-reduced-motion: reduce) {
		.box.sweep { animation: none; }
		.disc { transition: none; }
	}
</style>
