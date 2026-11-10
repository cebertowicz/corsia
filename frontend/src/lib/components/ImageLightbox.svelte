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
		clampScale, FIT_MARGIN, fitScale, fractionOnImage, isActualSize, percentLabel, readableOpen, scrollToHold,
		stepScale, type Point, type Size,
	} from '$lib/imageZoom';
	import { isMobile, prefersReducedMotion } from '$lib/stores/viewport';
	import { observeImageViewportPlace, resetImageViewportPlace, restoreImageViewportPlace } from '$lib/imageViewportPlace';
	import type { ImageViewportPlace } from '$lib/artifactTabs';
	import type { ImageRegion } from '$lib/imageRegions';
	import { attachImageWheel, imageViewerPointer } from '$lib/imageViewerPointer';

	let { src, alt, readableScale, docName = '', onBack, barActions, overlay, imageMargin = FIT_MARGIN, visibleBottom, pendingReveal = false, preserveSameSource = false, place = null, onPlace, onLoaded, onFailed, onRetry }: {
		src: string;
		alt: string;
		/** A scale to open at instead of Fit, for an image that is readable there (a diagram); Fit when the whole image fits at it. */
		readableScale?: number;
		/** The document this image belongs to; with `onBack` the bar offers a way back to it. */
		docName?: string;
		onBack?: () => void;
		/** Room in the bar for more actions, drawn before the zoom controls; `loaded` says the image is ready to act on. */
		barActions?: Snippet<[{ loaded: boolean }]>;
		/** Drawn over the image in a frame of its exact size, so it follows zoom and scrolling as part of the image. */
		overlay?: Snippet<[{ scale: number }]>;
		/** Space around the image, including any overlay extending beyond its edges. */
		imageMargin?: number;
		/** Bottom of the unobscured area in client coordinates, when a sheet overlays the image. */
		visibleBottom?: () => number | null;
		/** Keep a from-card subject hidden until its first positioning is complete. */
		pendingReveal?: boolean;
		/** A retained diagram snapshot keeps its load state when the document remaps its anchor. */
		preserveSameSource?: boolean;
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
	// Touch momentum is handled by imageViewerPointer.

	let viewportEl = $state<HTMLDivElement>();
	let imgEl = $state<HTMLImageElement>();
	let viewportSize = $state<Size>({ width: 0, height: 0 });
	let natural = $state<(Size & { restored?: true }) | null>(null);
	let failure = $state<string | null>(null);
	let attempt = $state(0);
	let free = $state<number | null>(null); // null is Fit and follows the viewport as it resizes.

	const fit = $derived(natural ? fitScale(natural, viewportSize, imageMargin) : 1);
	const scale = $derived(free === null ? fit : clampScale(free, fit));
	/** Fit is the state whenever the shown scale is Fit: `free` is null there, but a kept zoom the window has since outgrown clamps to Fit too. */
	const atFit = $derived(Math.abs(scale - fit) < 1e-6);
	const shown = $derived(natural ? { width: natural.width * scale, height: natural.height * scale } : null);
	const loaded = $derived(natural !== null && failure === null);
	let overflowing = $state(false);
	/** Keep a revealed place centred through resize until the reader moves the view. */
	let held: (Point & { width?: number; height?: number; scale?: number; subject?: ImageRegion; visible?: ImageRegion }) | null = null;
	let heldViewport = $state<Size | null>(null);
	let coveredHeight = $state(0);

	// A new image starts over at Fit, or at its readable scale when it has one and does not fit there.
	let previousSource: string | undefined;
	let previousAttempt = -1;
	$effect(() => {
		const nextSource = src;
		const nextAttempt = attempt;
		if (preserveSameSource && nextSource === previousSource && nextAttempt === previousAttempt) return;
		previousSource = nextSource;
		previousAttempt = nextAttempt;
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
			if (held && !regionVisible()) settleReveal('auto');
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
		free = saved ? saved.scale ?? null : readableOpen(readableScale, fit);
		await tick();
		if (saved && viewportEl) restoreImageViewportPlace(viewportEl, saved);
		natural = { width: el.naturalWidth || UNSIZED.width, height: el.naturalHeight || UNSIZED.height, restored: true };
		// The viewport stays hidden, which cannot take focus, until this update has drawn it.
		await tick();
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
	/** Fit a region and a small margin into the visible area before revealing it. */
	export function revealRegion(x: number, y: number, width: number, height: number, subject?: ImageRegion) {
		if (!viewportEl || !imgEl || !loaded) return;
		held = { x: x + width / 2, y: y + height / 2, width, height, scale, subject };
		heldViewport = { width: viewportEl.clientWidth, height: viewportEl.clientHeight };
		settleReveal('auto');
	}
	/** Glides an image fraction to the centre, keeping its zoom. */
	export function revealFraction(x: number, y: number) {
		if (!viewportEl || !imgEl || !loaded) return;
		heldViewport = null;
		held = { x, y };
		settleReveal($prefersReducedMotion ? 'auto' : 'smooth');
	}
	function regionVisible(): boolean {
		const el = viewportEl;
		const image = imgEl;
		const target = held;
		if (!el || !image || !target || target.width === undefined || target.height === undefined) return false;
		const view = el.getBoundingClientRect();
		const bounds = image.getBoundingClientRect();
		const bottom = Math.min(view.bottom, visibleBottom?.() ?? view.bottom);
		const margin = 12;
		const visible = target.visible ?? { x: target.x - target.width / 2, y: target.y - target.height / 2, w: target.width, h: target.height };
		const left = bounds.left + visible.x * bounds.width;
		const right = bounds.left + (visible.x + visible.w) * bounds.width;
		const top = bounds.top + visible.y * bounds.height;
		const end = bounds.top + (visible.y + visible.h) * bounds.height;
		return left >= view.left + margin && right <= view.right - margin && top >= view.top + margin && end <= bottom - margin;
	}
	function settleReveal(behavior: ScrollBehavior) {
		const el = viewportEl;
		const target = held;
		if (!el || !target || !natural) return;
		if (target.scale !== undefined) {
			const view = el.getBoundingClientRect();
			const bottom = Math.max(view.top, Math.min(view.bottom, visibleBottom?.() ?? view.bottom));
			// Scroll offsets are quantized by the browser; leave the existing 12px visible margin intact.
			const margin = 13;
			const box = { x: target.x - (target.width ?? 0) / 2, y: target.y - (target.height ?? 0) / 2, w: target.width ?? 0, h: target.height ?? 0 };
			const subject = target.subject ?? box;
			const horizontal = subject.w ? Math.max(1, view.width - 2 * margin) / (natural.width * subject.w) : target.scale;
			const vertical = subject.h ? Math.max(1, bottom - view.top - 2 * margin) / (natural.height * subject.h) : target.scale;
			const next = Math.max(fit, Math.min(target.scale, horizontal, vertical));
			free = Math.abs(next - fit) < 1e-6 ? null : next;
			flushSync();
			const availableWidth = Math.max(1, view.width - 2 * margin) / (natural.width * next);
			const availableHeight = Math.max(1, bottom - view.top - 2 * margin) / (natural.height * next);
			target.visible = subject;
			const centre = (start: number, length: number, desired: number, available: number) => Math.max(start + length - available / 2, Math.min(start + available / 2, desired));
			const x = centre(box.x, box.w, subject.x + subject.w / 2, availableWidth);
			const y = centre(box.y, box.h, subject.y + subject.h / 2, availableHeight);
			centreOn(x, y, behavior);
			return;
		}
		centreOn(target.x, target.y, behavior);
	}
	function centreOn(x: number, y: number, behavior: ScrollBehavior) {
		const el = viewportEl;
		const image = imgEl;
		if (!el || !image || !loaded) return;
		const at = { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) };
		const view = el.getBoundingClientRect();
		const bottom = Math.max(view.top, Math.min(view.bottom, visibleBottom?.() ?? view.bottom));
		coveredHeight = view.bottom - bottom;
		flushSync();
		const delta = scrollToHold(at, { x: view.left + view.width / 2, y: view.top + (bottom - view.top) / 2 }, image.getBoundingClientRect());
		el.scrollTo({ left: el.scrollLeft + delta.x, top: el.scrollTop + delta.y, behavior });
	}
	/** Re-check the subject when an overlay appears or disappears without resizing the viewport. */
	export function refreshCoveredArea() { if (held && !regionVisible()) settleReveal('auto'); }
	function releaseReveal() { held = null; heldViewport = null; }
	/** The covering sheet has closed. */
	export function clearSheet() { coveredHeight = 0; releaseReveal(); }
	function toFit() {
		releaseReveal();
		free = null;
		resetImageViewportPlace(viewportEl, free, onPlace);
	}
	const zoomIn = () => { releaseReveal(); zoomTo(stepScale(scale, 1, fit)); };
	const zoomOut = () => { releaseReveal(); zoomTo(stepScale(scale, -1, fit)); };

	// Wheel and WebKit gesture events must be able to cancel the page's own zoom, so they cannot be passive.
	$effect(() => {
		const el = viewportEl;
		if (el) return attachImageWheel(el, {
			releaseReveal, loaded: () => loaded, scale: () => scale, image: () => imgEl, zoomTo, placeScale,
		});
	});

	let panning = $state(false);
	const pointer = imageViewerPointer({
		viewport: () => viewportEl, image: () => imgEl, loaded: () => loaded,
		overflowing: () => overflowing, reducedMotion: () => $prefersReducedMotion,
		scale: () => scale, releaseReveal, placeScale, setPanning: (value) => { panning = value; },
	});

	function onKeydown(event: KeyboardEvent) {
		releaseReveal();
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
		<div class="zoom" data-image-zoom role="group" aria-label="Zoom">
			<button type="button" class="control" aria-pressed={loaded && atFit} disabled={!loaded} onclick={toFit}>Fit</button>
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
			<button type="button" class="control" onclick={retry}>Retry</button>
		</div>
	{:else}
		{#if natural === null}<div class="loadbar"><span></span></div>{/if}
		<!-- The focusable region takes zoom keys and touch input; its role names it for assistive tech. -->
		<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
		<div
			class="viewport"
			class:pannable={overflowing}
			class:panning
			class:pending={natural === null || !natural.restored || pendingReveal}
			bind:this={viewportEl}
			role="region"
			aria-label={alt ? `Image: ${alt}` : 'Image'}
			tabindex="0"
			onpointerdown={pointer.down}
			onpointermove={pointer.move}
			onpointerup={pointer.end}
			onpointercancel={pointer.end}
			onkeydown={onKeydown}
		>
			<div class="pad" style:--image-margin="{imageMargin}px" style:--covered-height="{coveredHeight}px" style:--pad-min-width={heldViewport ? `${heldViewport.width}px` : '100%'} style:--pad-min-height={heldViewport ? `${heldViewport.height}px` : '100%'}>
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
	/* The document's name is the one label that yields: it truncates before any bar control is pushed past the edge. */
	.back { padding-left: 4px; max-width: 60%; min-width: 0; }
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
	.pad { --pad-min-width: 100%; --pad-min-height: 100%; --image-margin: 0px; --covered-height: 0px; display: flex; box-sizing: border-box; width: max-content; min-width: var(--pad-min-width); min-height: var(--pad-min-height); padding: var(--image-margin); padding-bottom: calc(var(--image-margin) + var(--covered-height)); }
	.frame {
		position: relative; flex: none; margin: auto;
		border-radius: 2px; box-shadow: 0 0 0 1px var(--border), 0 1px 4px color-mix(in srgb, #000 28%, transparent);
	}
	.frame img { display: block; width: 100%; height: 100%; max-width: none; border-radius: inherit; user-select: none; -webkit-user-drag: none; }

	.load-error { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; padding: 24px 4px; color: var(--text-secondary); }
	.load-error p { margin: 0; font-size: var(--text-subhead); color: var(--red); font-weight: var(--fw-medium); }
	.load-error code { font-size: var(--text-caption); color: var(--text-tertiary); word-break: break-all; }
	.load-error :global(.control) { background: var(--amber-bg); color: var(--amber-text); }

	/* Responsive control layout is isolated in ImageLightboxResponsive.css. */
</style>
