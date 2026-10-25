<script lang="ts">
	import './ImageLightboxResponsive.css';
	// The canvas image viewer: one image at Fit, zoomed and panned. Shared by an image artifact and an image of a
	// document. The image only ever reaches the page through <img>, so an SVG cannot run script. The scroll
	// container is the browser's own, so scrollbars, trackpad scrolling and keyboard scrolling pan it natively;
	// zoom and touch panning are applied here, around the point under the pointer.
	import './DocViewer.css';
	import { flushSync, tick, type Snippet } from 'svelte';
	import { FAILED_IMAGE, imageFailureReason } from '$lib/imageFailure';
	import {
		clampScale, distance, fitScale, fractionOnImage, isActualSize, midpoint, percentLabel, scrollToHold,
		stepScale, wheelFactor, type Point, type Size,
	} from '$lib/imageZoom';
	import { isMobile, prefersReducedMotion } from '$lib/stores/viewport';
	import { observeImageViewportPlace, resetImageViewportPlace, restoreImageViewportPlace } from '$lib/imageViewportPlace';
	import type { ImageViewportPlace } from '$lib/artifactTabs';

	let { src, alt, docName = '', onBack, barActions, overlay, place = null, onPlace, onLoaded, onFailed, onRetry }: {
		src: string;
		alt: string;
		/** The document this image belongs to; with `onBack` the bar offers a way back to it. */
		docName?: string;
		onBack?: () => void;
		/** Room in the bar for more actions, drawn before the zoom controls; `loaded` says the image is ready to act on. */
		barActions?: Snippet<[{ loaded: boolean }]>;
		/** Drawn over the image in a frame of its exact size, so it follows zoom and scrolling as part of the image. */
		overlay?: Snippet<[{ scale: number }]>;
		/** The in-session image camera, including user-chosen free zoom or Fit. */
		place?: ImageViewportPlace | null;
		onPlace?: (place: ImageViewportPlace) => void;
		onLoaded?: () => void;
		onFailed?: () => void;
		/** The host's own reload for a failed image; without it the same address is requested again. */
		onRetry?: () => void;
	} = $props();

	// Fallback for an image with no intrinsic size (an SVG with only a viewBox).
	const UNSIZED: Size = { width: 800, height: 600 };
	const FLING_FRICTION = 0.94;

	let viewportEl = $state<HTMLDivElement>();
	let imgEl = $state<HTMLImageElement>();
	let viewportSize = $state<Size>({ width: 0, height: 0 });
	let natural = $state<(Size & { restored?: true }) | null>(null);
	let failure = $state<string | null>(null);
	let attempt = $state(0);
	let free = $state<number | null>(null); // null is Fit and follows the viewport as it resizes.

	const fit = $derived(natural ? fitScale(natural, viewportSize) : 1);
	const scale = $derived(free === null ? fit : clampScale(free, fit));
	const shown = $derived(natural ? { width: natural.width * scale, height: natural.height * scale } : null);
	const loaded = $derived(natural !== null && failure === null);
	let overflowing = $state(false);

	// A new image starts over at Fit.
	$effect(() => {
		src;
		attempt;
		natural = null;
		failure = null;
		free = null;
	});

	$effect(() => {
		const el = viewportEl;
		if (!el) return;
		const measure = () => {
			viewportSize = { width: el.clientWidth, height: el.clientHeight };
			overflowing = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
		};
		measure();
		if (typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(measure);
		observer.observe(el);
		return () => observer.disconnect();
	});
	// The scrollable size follows the drawn image, which the observer on the viewport does not see.
	$effect(() => {
		shown;
		const el = viewportEl;
		if (el) overflowing = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
	});

	$effect(() => {
		const el = viewportEl;
		if (el && onPlace) return observeImageViewportPlace(el, onPlace, () => free);
	});

	async function onImageLoad() {
		const el = imgEl;
		if (!el) return;
		natural = el.naturalWidth > 0 && el.naturalHeight > 0 ? { width: el.naturalWidth, height: el.naturalHeight } : UNSIZED;
		await tick();
		const saved = place;
		if (saved) free = saved.scale ?? null;
		await tick();
		if (saved && viewportEl) restoreImageViewportPlace(viewportEl, saved);
		natural = { width: el.naturalWidth || UNSIZED.width, height: el.naturalHeight || UNSIZED.height, restored: true };
		if (onBack) viewportEl?.focus({ preventScroll: true });
		onLoaded?.();
	}
	async function onImageError() {
		const failedSrc = src;
		const failedAttempt = attempt;
		onFailed?.();
		failure = FAILED_IMAGE;
		const reason = await imageFailureReason(failedSrc);
		if (src === failedSrc && attempt === failedAttempt) failure = reason;
	}
	function retry() {
		if (onRetry) onRetry();
		else attempt += 1;
	}

	/** Sets the scale and scrolls so the image fraction `at` stays under the client point `pivot`. */
	function placeScale(next: number, at: Point, pivot: Point) {
		const el = viewportEl;
		const image = imgEl;
		if (!el || !image) return;
		const target = clampScale(next, fit);
		free = Math.abs(target - fit) < 1e-6 ? null : target;
		flushSync();
		const delta = scrollToHold(at, pivot, image.getBoundingClientRect());
		el.scrollLeft += delta.x;
		el.scrollTop += delta.y;
		onPlace?.({ scrollLeft: el.scrollLeft, scrollTop: el.scrollTop, scale: free });
	}
	function center(): Point {
		const box = viewportEl!.getBoundingClientRect();
		return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
	}
	function zoomTo(next: number, pivot: Point = center()) {
		if (!imgEl) return;
		placeScale(next, fractionOnImage(pivot, imgEl.getBoundingClientRect()), pivot);
	}
	/** The shown image's live bounds, for an anchored card to avoid covering its tags. */
	export function imageBounds(): DOMRect | null { return imgEl?.isConnected ? imgEl.getBoundingClientRect() : null; }
	/** The image's own pixel size once it has loaded, for a caller sizing the space around the viewer. */
	export function naturalSize(): Size | null { return natural; }
	/** Glides the given fraction of the image (0..1 on each axis) to the viewport's centre, keeping the zoom; at once with reduced motion. */
	export function revealFraction(x: number, y: number) {
		const el = viewportEl;
		const image = imgEl;
		if (!el || !image || !loaded) return;
		const at = { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) };
		const view = el.getBoundingClientRect();
		const delta = scrollToHold(at, { x: view.left + view.width / 2, y: view.top + view.height / 2 }, image.getBoundingClientRect());
		el.scrollTo({ left: el.scrollLeft + delta.x, top: el.scrollTop + delta.y, behavior: $prefersReducedMotion ? 'auto' : 'smooth' });
	}
	function toFit() {
		free = null;
		resetImageViewportPlace(viewportEl, free, onPlace);
	}
	const zoomIn = () => zoomTo(stepScale(scale, 1, fit));
	const zoomOut = () => zoomTo(stepScale(scale, -1, fit));

	// Wheel and WebKit gesture events must be able to cancel the page's own zoom, so they cannot be passive.
	$effect(() => {
		const el = viewportEl;
		if (!el) return;
		const onWheel = (event: WheelEvent) => {
			if (!event.ctrlKey && !event.metaKey) return;
			event.preventDefault();
			if (loaded) zoomTo(scale * wheelFactor(event.deltaY, event.deltaMode), { x: event.clientX, y: event.clientY });
		};
		let gesture: { scale: number; at: Point } | null = null;
		type GestureEvent = Event & { scale: number; clientX: number; clientY: number };
		const gestureStart = (event: Event) => {
			event.preventDefault();
			const e = event as GestureEvent;
			if (imgEl) gesture = { scale, at: fractionOnImage({ x: e.clientX, y: e.clientY }, imgEl.getBoundingClientRect()) };
		};
		const gestureChange = (event: Event) => {
			event.preventDefault();
			const e = event as GestureEvent;
			if (gesture && loaded) placeScale(gesture.scale * e.scale, gesture.at, { x: e.clientX, y: e.clientY });
		};
		const gestureEnd = (event: Event) => { event.preventDefault(); gesture = null; };
		el.addEventListener('wheel', onWheel, { passive: false });
		el.addEventListener('gesturestart', gestureStart);
		el.addEventListener('gesturechange', gestureChange);
		el.addEventListener('gestureend', gestureEnd);
		return () => {
			el.removeEventListener('wheel', onWheel);
			el.removeEventListener('gesturestart', gestureStart);
			el.removeEventListener('gesturechange', gestureChange);
			el.removeEventListener('gestureend', gestureEnd);
		};
	});

	// Touch has no scroll of its own here (touch-action is none, so one finger and two are ours to read).
	const pointers = new Map<number, Point>();
	let pinch: { distance: number; scale: number; at: Point } | null = null;
	let drag: { last: Point; velocity: Point; time: number } | null = null;
	let fling = 0;
	let panning = $state(false);

	const stopFling = () => { cancelAnimationFrame(fling); fling = 0; };
	function startFling(velocity: Point) {
		const el = viewportEl;
		if (!el || $prefersReducedMotion || Math.hypot(velocity.x, velocity.y) < 0.3) return;
		let v = velocity;
		let previous = performance.now();
		const step = (now: number) => {
			const dt = Math.min(48, now - previous);
			previous = now;
			el.scrollLeft -= v.x * dt;
			el.scrollTop -= v.y * dt;
			v = { x: v.x * FLING_FRICTION ** (dt / 16), y: v.y * FLING_FRICTION ** (dt / 16) };
			fling = Math.hypot(v.x, v.y) > 0.02 ? requestAnimationFrame(step) : 0;
		};
		fling = requestAnimationFrame(step);
	}

	function beginPinch() {
		const [a, b] = [...pointers.values()];
		if (!imgEl) return;
		const mid = midpoint(a, b);
		pinch = { distance: Math.max(1, distance(a, b)), scale, at: fractionOnImage(mid, imgEl.getBoundingClientRect()) };
		drag = null;
	}
	function onPointerDown(event: PointerEvent) {
		if (!loaded || (event.pointerType === 'mouse' && (event.button !== 0 || !overflowing))) return;
		stopFling();
		viewportEl?.setPointerCapture(event.pointerId);
		pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
		if (pointers.size === 2) beginPinch();
		else if (pointers.size === 1) {
			drag = { last: { x: event.clientX, y: event.clientY }, velocity: { x: 0, y: 0 }, time: event.timeStamp };
			panning = event.pointerType === 'mouse';
		}
	}
	function onPointerMove(event: PointerEvent) {
		const el = viewportEl;
		if (!el || !pointers.has(event.pointerId)) return;
		pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
		if (pinch && pointers.size >= 2) {
			const [a, b] = [...pointers.values()];
			placeScale(pinch.scale * (distance(a, b) / pinch.distance), pinch.at, midpoint(a, b));
		} else if (drag) {
			const here = { x: event.clientX, y: event.clientY };
			const dx = here.x - drag.last.x;
			const dy = here.y - drag.last.y;
			el.scrollLeft -= dx;
			el.scrollTop -= dy;
			const dt = Math.max(1, event.timeStamp - drag.time);
			drag = { last: here, velocity: { x: dx / dt, y: dy / dt }, time: event.timeStamp };
		}
	}
	function onPointerEnd(event: PointerEvent) {
		if (!pointers.delete(event.pointerId)) return;
		panning = false;
		if (pointers.size === 0) {
			const flung = drag && event.pointerType === 'touch' && event.type === 'pointerup' ? drag.velocity : null;
			pinch = null;
			drag = null;
			if (flung) startFling(flung);
		} else if (pointers.size === 1) {
			// One finger of a pinch lifts: the other carries on as a pan from where it is.
			const [rest] = [...pointers.values()];
			pinch = null;
			drag = { last: rest, velocity: { x: 0, y: 0 }, time: event.timeStamp };
		}
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.ctrlKey || event.metaKey || event.altKey || !loaded) return;
		if (event.key === '+' || event.key === '=') zoomIn();
		else if (event.key === '-') zoomOut();
		else if (event.key === '0') toFit();
		else if (event.key === '1') zoomTo(1);
		else return;
		event.preventDefault();
	}
	function onWindowKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape' || event.defaultPrevented || !onBack) return;
		event.preventDefault();
		onBack();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="lightbox image-lightbox">
	<div class="bar">
		{#if onBack}
			<button type="button" class="control back" onclick={onBack} aria-label={docName ? `Back to ${docName}` : 'Back'}>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
				<span class="back-text">{$isMobile || !docName ? 'Back' : `Back to ${docName}`}</span>
			</button>
		{/if}
		<span class="spacer"></span>
		{@render barActions?.({ loaded })}
		<div class="zoom" role="group" aria-label="Zoom">
			<button type="button" class="control" aria-pressed={free === null} disabled={!loaded} onclick={toFit}>Fit</button>
			{#if !$isMobile}
				<button type="button" class="control" aria-pressed={loaded && isActualSize(scale)} disabled={!loaded} onclick={() => zoomTo(1)}>100%</button>
				<button type="button" class="control icon" aria-label="Zoom out" disabled={!loaded || scale <= fit + 1e-6} onclick={zoomOut}>
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 12h14" /></svg>
				</button>
				<span class="level" aria-live="polite">{loaded ? percentLabel(scale) : ''}</span>
				<button type="button" class="control icon" aria-label="Zoom in" disabled={!loaded || scale >= 8 - 1e-6} onclick={zoomIn}>
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
				</button>
			{/if}
		</div>
	</div>

	{#if failure !== null}
		<div class="load-error" role="alert">
			<p>Couldn't load this image.</p>
			{#if failure !== FAILED_IMAGE}<code>{alt ? `${alt} · ${failure}` : failure}</code>{/if}
			<button type="button" class="control" onclick={retry}>Try again</button>
		</div>
	{:else}
		{#if natural === null}<div class="loadbar"><span></span></div>{/if}
		<!-- The focusable region takes zoom keys and touch input; its role names it for assistive tech. -->
		<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
		<div
			class="viewport"
			class:pannable={overflowing}
			class:panning
			class:pending={natural === null || !natural.restored}
			bind:this={viewportEl}
			role="region"
			aria-label={alt ? `Image: ${alt}` : 'Image'}
			tabindex="0"
			onpointerdown={onPointerDown}
			onpointermove={onPointerMove}
			onpointerup={onPointerEnd}
			onpointercancel={onPointerEnd}
			onkeydown={onKeydown}
		>
			<div class="pad">
				<div class="frame" style={shown ? `width:${shown.width}px;height:${shown.height}px` : undefined}>
					{#key attempt}
						<img bind:this={imgEl} {src} {alt} draggable="false" onload={onImageLoad} onerror={onImageError} />
					{/key}
					{#if loaded}{@render overlay?.({ scale })}{/if}
				</div>
			</div>
		</div>
	{/if}
</div>

<style>
	.lightbox { position: relative; flex: 1; min-height: 0; min-width: 0; height: 100%; display: flex; flex-direction: column; box-sizing: border-box; }
	.bar { display: flex; align-items: center; gap: 4px; flex-shrink: 0; min-height: 36px; padding: 0 0 6px; }
	.spacer { flex: 1; }
	.zoom { display: flex; align-items: center; gap: 2px; }
	.level { min-width: 4ch; text-align: center; font-size: var(--text-footnote); font-variant-numeric: tabular-nums; color: var(--text-tertiary); }
	/* The one control look for the whole bar, including actions drawn into it, so they read and respond as one row. */
	.lightbox :global(.control) {
		display: inline-flex; align-items: center; justify-content: center; gap: 4px; height: 28px; padding: 0 8px; border: 0;
		border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary);
		font-family: var(--font-sans); font-size: var(--text-footnote); font-weight: var(--fw-medium); white-space: nowrap; cursor: pointer;
		transition: background-color var(--dur-tight) var(--ease-snap), color var(--dur-tight) var(--ease-snap);
	}
	.lightbox :global(.control.icon) { width: 28px; padding: 0; }
	/* Hover only where a pointer hovers, and never over a pressed, open or held control, so the accent and press tint show. */
	@media (hover: hover) {
		.lightbox :global(.control:hover:not(:disabled):not(:active):not([aria-pressed='true']):not([aria-expanded='true'])) { background: var(--bg-hover); color: var(--text-primary); }
	}
	.lightbox :global(.control[aria-pressed='true']), .lightbox :global(.control[aria-expanded='true']) { background: var(--accent-wash); color: var(--accent-text); }
	.lightbox :global(.control:disabled) { opacity: 0.4; cursor: default; }
	/* Press-down tints at once, in place of the app's press scale. */
	.lightbox :global(.control:not(:disabled):active) { scale: none; background: var(--press-bg); transition-duration: 0s; }
	.lightbox :global(.control:focus-visible), .viewport:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.back { padding-left: 4px; max-width: 60%; }
	.back-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

	/* A neutral backdrop, so a screenshot of a UI never reads as the app around it. */
	.viewport {
		flex: 1; min-height: 0; min-width: 0; overflow: auto; position: relative;
		background: var(--bg-secondary); border-radius: var(--radius-sm);
		touch-action: none; overscroll-behavior: contain;
	}
	.viewport.pending { visibility: hidden; }
	.viewport.pannable { cursor: grab; }
	.viewport.panning { cursor: grabbing; user-select: none; }
	/* Auto margins centre a smaller image and, unlike justify-content, never clip the start of a larger one. */
	.pad { display: flex; box-sizing: border-box; width: max-content; min-width: 100%; min-height: 100%; padding: 24px; }
	.frame {
		position: relative; flex: none; margin: auto;
		border-radius: 2px; box-shadow: 0 0 0 1px var(--border), 0 1px 4px color-mix(in srgb, #000 28%, transparent);
	}
	.frame img { display: block; width: 100%; height: 100%; max-width: none; border-radius: inherit; user-select: none; -webkit-user-drag: none; }

	.load-error { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; padding: 24px 4px; color: var(--text-secondary); }
	.load-error p { margin: 0; font-size: var(--text-subhead); color: var(--red); font-weight: var(--fw-medium); }
	.load-error code { font-size: var(--text-caption); color: var(--text-tertiary); word-break: break-all; }
	.load-error :global(.control) { background: var(--amber-bg); color: var(--amber-text); }
</style>
