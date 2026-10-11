<script lang="ts">
	// The numbered tags, boxes and point pins over an image in the viewer
	// (DCOL-FR13.3, FR13.4, FR13.9), and the surface a box or point is drawn on.
	// It fills the viewer's frame, which is the image at its shown size, so
	// regions are placed in fractions and tags keep their size at any zoom.
	import { onMount } from 'svelte';
	import type { DocComment } from '$lib/stores/docComments';
	import {
		describeRegion, isPoint, nearestTarget, placeTags, regionFromDrag, regionRect, type ImageRegion, type Point,
	} from '$lib/imageRegions';

	/** `movedFrom` is where a pin being moved was, while its new place saves. */
	type Pinned = DocComment & { region: ImageRegion; regionNumber: number; movedFrom?: ImageRegion };
	let { comments, colorOf, draftColor, draft = null, moving = null, selectedId = null, hoveredId = null, boxMode = false, cardOpen = false, readOnly = false, scale = 1, onPick, onHover, onDraw, onDismiss }: {
		/** Region comments to show; a resolved one shows only while it is selected. */
		comments: DocComment[];
		colorOf: (comment: DocComment) => string;
		/** The colour a new comment by the person takes. */
		draftColor: string;
		/** The region of the comment being written. */
		draft?: ImageRegion | null;
		/** The pin being moved, and once drawn, the new place it is saving to. */
		moving?: { id: string; region: ImageRegion | null } | null;
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
	let spaceHeld = $state(false);
	let stroke = $state<{ pointer: number; from: Point; to: Point; closes: boolean } | null>(null);
	let focusedTag = $state<string | null>(null);
	// Agent comments that arrive while the viewer is open sweep in; those already there do not.
	let known: Set<string> | null = null;
	let sweeping = $state<Set<string>>(new Set());
	/** Agent labels stay present for their measured footprint, but hide at rest when they cover another tag, label or point. */
	let hiddenAgentNames = $state<Set<string>>(new Set());

	const pinned = $derived(
		comments
			.filter((comment): comment is Pinned => !!comment.region && comment.regionNumber != null && (!comment.resolved || comment.id === selectedId))
			.map((comment) => (moving?.region && comment.id === moving.id ? { ...comment, region: moving.region, movedFrom: comment.region } : comment))
			.sort((a, b) => a.regionNumber - b.regionNumber),
	);
	const size = $derived({ width, height });
	const tags = $derived.by(() => {
		if (!width || !height) return [];
		const positions = placeTags(pinned.map((comment) => comment.region), size);
		return pinned.map((comment, index) => ({ comment, ...positions[index] }));
	});
	const preview = $derived(stroke ? regionFromDrag(stroke.from, stroke.to, size) : draft);
	const intersects = (a: DOMRect, b: DOMRect) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
	const sameIds = (a: Set<string>, b: Set<string>) => a.size === b.size && [...a].every((id) => b.has(id));
	function showAgentName(comment: Pinned) {
		return comment.authorKind === 'agent' && (comment.id === selectedId || comment.id === hoveredId || comment.id === focusedTag || !hiddenAgentNames.has(comment.id));
	}

	$effect(() => {
		void tags;
		if (!layer) return;
		const tagsOnScreen = [...layer.querySelectorAll<HTMLElement>('[data-region-tag]')];
		const dots = [...layer.querySelectorAll<HTMLElement>('[data-region-dot]')];
		const hidden = new Set<string>();
		for (const tag of tagsOnScreen) {
			const name = tag.querySelector<HTMLElement>('.name');
			const id = tag.dataset.commentId;
			if (!name || !id) continue;
			const nameRect = name.getBoundingClientRect();
			const covers = (other: HTMLElement) => intersects(nameRect, other.getBoundingClientRect());
			if (tagsOnScreen.some((other) => other !== tag && covers(other)) || dots.some((dot) => dot.dataset.regionDot !== id && covers(dot))) hidden.add(id);
		}
		if (!sameIds(hiddenAgentNames, hidden)) hiddenAgentNames = hidden;
	});

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
		// A pin saving at its new place is not picked: on a phone, the tap that placed it also lands on it as a click.
		if (comment && !comment.movedFrom) onPick(chosen, anchorFor(comment.region), event.currentTarget as HTMLElement);
	}
	function onSurfaceKey(event: KeyboardEvent) {
		if (event.key !== 'Enter' || readOnly) return;
		event.preventDefault();
		const whole = { x: 0, y: 0, w: 1, h: 1 };
		onDraw(whole, anchorFor(whole));
	}

	const pct = (value: number) => `${value * 100}%`;
	const label = (comment: Pinned) => `Comment ${comment.regionNumber} by ${comment.authorKind === 'agent' ? comment.author : 'you'}, ${describeRegion(comment.region)}${comment.movedFrom ? ', moving here' : comment.regionStale ? ', image changed since this comment' : ''}`;
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="regions"
	class:drawing={!readOnly}
	class:space-held={spaceHeld}
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
		{@const pending = !!comment.movedFrom}
		{@const shown = pending || comment.id === selectedId || comment.id === hoveredId || comment.id === focusedTag || sweeping.has(comment.id)}
		{#if comment.movedFrom}
			<!-- Where the pin was, dashed until its new place saves or the move fails. -->
			{#if isPoint(comment.movedFrom)}
				<span class="dot leaving" style:left={pct(comment.movedFrom.x)} style:top={pct(comment.movedFrom.y)} style:--region-color={colorOf(comment)} aria-hidden="true" data-region-from={comment.id}></span>
			{:else}
				<span class="box shown leaving" style:left={pct(comment.movedFrom.x)} style:top={pct(comment.movedFrom.y)} style:width={pct(comment.movedFrom.w)} style:height={pct(comment.movedFrom.h)} style:--region-color={colorOf(comment)} aria-hidden="true" data-region-from={comment.id}></span>
			{/if}
		{/if}
		{#if isPoint(comment.region)}
			<span class="dot" class:faint={!shown} class:pending class:stale={!pending && comment.regionStale} style:left={pct(comment.region.x)} style:top={pct(comment.region.y)} style:--region-color={colorOf(comment)} aria-hidden="true" data-region-dot={comment.id}></span>
		{:else}
			<!-- Every box stays mounted, so it fades in on hover, focus or pick rather than popping. -->
			<span class="box" class:shown class:pending class:stale={!pending && comment.regionStale} class:sweep={sweeping.has(comment.id)} style:left={pct(comment.region.x)} style:top={pct(comment.region.y)} style:width={pct(comment.region.w)} style:height={pct(comment.region.h)} style:--region-color={colorOf(comment)} aria-hidden="true" data-region-box={comment.id}></span>
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
			class:stale={!comment.movedFrom && comment.regionStale}
			class:pending={!!comment.movedFrom}
			class:leftward={side === 'left' || side === 'up-left' || side === 'down-left'}
			style:left="{x}px"
			style:top="{y}px"
			style:--region-color={colorOf(comment)}
			aria-label={label(comment)}
			aria-pressed={comment.id === selectedId}
			data-region-tag={comment.regionNumber}
			data-comment-id={comment.id}
			onpointerdown={onTagDown}
			onclick={(event) => pick(event, comment.id)}
			onpointerenter={() => onHover(comment.id)}
			onpointerleave={() => onHover(null)}
			onfocus={() => { focusedTag = comment.id; onHover(comment.id); }}
			onblur={() => { if (focusedTag === comment.id) focusedTag = null; onHover(null); }}
		><span class="disc">{comment.regionNumber}</span>{#if agent}<span class="name" class:hidden={!showAgentName(comment)}>{comment.author}</span>{/if}</button>
	{/each}
</div>

<style>
	.regions { position: absolute; inset: 0; z-index: 1; }
	@media (pointer: fine) {
		.regions.drawing { cursor: crosshair; }
		.regions.drawing.space-held { cursor: grab; }
	}
	.surface { position: absolute; inset: 0; border-radius: inherit; }
	.surface:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.regions.box-mode { box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--accent) 55%, transparent); }

	/* A box is the author's colour inside a thin white line, so it reads on light and dark parts of a screenshot. */
	.box {
		position: absolute; box-sizing: border-box; border: 2px solid var(--region-color); border-radius: var(--radius-2xs);
		box-shadow: 0 0 0 1px var(--image-region-halo), inset 0 0 0 1px var(--image-region-halo); pointer-events: none;
		opacity: 0; transition: opacity calc(var(--dur-tight) * 0.7) var(--ease-snap);
	}
	.box.shown, .box.draft { opacity: 1; transition-duration: var(--dur-tight); }
	.box.stale { border-style: dashed; }
	.box.draft { border-style: solid; background: color-mix(in srgb, var(--region-color) 10%, transparent); }
	/* A pin being moved: the new place is dashed over a light fill until it saves; the old place is dashed and dimmed. */
	.box.pending { border-style: dashed; background: color-mix(in srgb, var(--region-color) 10%, transparent); }
	.box.shown.leaving { border-style: dashed; opacity: 0.55; }
	.box.sweep { animation: sweep 600ms cubic-bezier(0.22, 1, 0.36, 1); }
	@keyframes sweep { from { clip-path: inset(0 100% 100% 0); } to { clip-path: inset(0 0 0 0); } }

	/* A point pin is a 14 px dot. */
	.dot { position: absolute; width: 14px; height: 14px; margin: -7px 0 0 -7px; border-radius: 50%; background: var(--region-color); box-shadow: 0 0 0 2px var(--image-region-halo); pointer-events: none; }
	.dot.faint { background: color-mix(in srgb, var(--region-color) 80%, transparent); }
	.dot.stale { outline: 1.5px dashed var(--region-color); outline-offset: 3px; }
	.dot.pending { outline: 1.5px dashed var(--region-color); outline-offset: 3px; }
	.dot.leaving { background: transparent; border: 1.5px dashed var(--region-color); box-sizing: border-box; opacity: 0.7; }

	/* The 22 px disc sits on the placed centre; an agent's name grows away from the box it points at. */
	.tag {
		position: absolute; display: flex; align-items: center; gap: 4px; padding: 0; border: 0; background: none;
		transform: translate(-11px, -11px); font: inherit; cursor: pointer; -webkit-tap-highlight-color: transparent;
	}
	/* Only the disc answers a press, so the label and the 44 px target stay put. */
	.tag:active { scale: none; }
	/* A hovered, focused or picked tag rises over its neighbours, so a close one never covers its name. */
	.tag:not(.faint) { z-index: 1; }
	.tag.selected { z-index: 2; }
	.tag.leftward { flex-direction: row-reverse; transform: translate(calc(-100% + 11px), -11px); }
	/* An invisible 44 px target around the disc; overlapping ones go to the nearer centre. */
	.tag::before { content: ''; position: absolute; left: -11px; top: -11px; width: 44px; height: 44px; }
	.tag.leftward::before { left: auto; right: -11px; }
	.disc {
		position: relative; box-sizing: border-box; display: grid; place-items: center; width: 22px; height: 22px; border-radius: var(--radius-circle);
		background: var(--region-color); color: var(--image-region-ink); border: 1.5px solid color-mix(in srgb, var(--region-color) 70%, var(--image-region-edge));
		box-shadow: 0 0 0 1.5px var(--image-region-halo); font-size: var(--text-caption); font-weight: var(--fw-bold); font-variant-numeric: tabular-nums; line-height: 1;
		transition: background var(--dur-tight) var(--ease-snap), scale var(--dur-tight) var(--ease-snap);
	}
	/* The selected ring is its own layer, so it fades in with opacity rather than an animated shadow. */
	.disc::after {
		content: ''; position: absolute; inset: -5.5px; border-radius: var(--radius-circle); pointer-events: none;
		box-shadow: inset 0 0 0 2.5px var(--region-color); opacity: 0; transition: opacity calc(var(--dur-tight) * 0.7) var(--ease-snap);
	}
	.tag.selected .disc::after { opacity: 1; transition-duration: var(--dur-tight); }
	.name {
		max-width: 14ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 2px 6px; border-radius: var(--radius-pill);
		background: var(--region-color); color: var(--image-region-ink); border: 1.5px solid color-mix(in srgb, var(--region-color) 70%, var(--image-region-edge)); box-shadow: 0 0 0 1.5px var(--image-region-halo);
		font-size: var(--text-caption); font-weight: var(--fw-semibold); line-height: 14px;
		transition: opacity var(--dur-tight) var(--ease-snap), background var(--dur-tight) var(--ease-snap);
	}
	.name.hidden { opacity: 0; pointer-events: none; transition-duration: calc(var(--dur-tight) * 0.7); }
	/* Faint until hovered, focused or picked: only the fill fades; the digit, border and white ring stay solid. */
	.tag.faint .disc, .tag.faint .name { background: color-mix(in srgb, var(--region-color) 80%, transparent); }
	.tag.stale .disc, .tag.stale .name, .tag.pending .disc, .tag.pending .name { border-style: dashed; background: color-mix(in srgb, var(--region-color) 80%, transparent); }
	.tag:hover .disc, .tag:hover .name { background: var(--region-color); }
	.tag:focus-visible { outline: none; }
	.tag:focus-visible .disc { background: var(--region-color); outline: 2px solid var(--accent); outline-offset: 3px; }
	/* Held: the disc tints and compresses at once; last, so it wins over hover. */
	.tag:active .disc { scale: 0.97; background: color-mix(in srgb, var(--region-color) 82%, #000); transition-duration: 0s; }
	@media (prefers-reduced-motion: reduce) {
		.box.sweep { animation: none; }
		/* Fades stay as short crossfades; a press only tints. */
		.tag:active .disc { scale: none; }
	}
</style>
