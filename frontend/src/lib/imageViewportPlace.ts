import type { ImageViewportPlace } from '$lib/artifactTabs';

/** Restore the image camera offsets after its saved scale has been applied. */
export function restoreImageViewportPlace(el: HTMLElement, place: ImageViewportPlace): void {
	el.scrollLeft = place.scrollLeft;
	el.scrollTop = place.scrollTop;
}

/** Reset an image viewport to Fit's origin and retain that camera sample when requested. */
export function resetImageViewportPlace(
	el: HTMLElement | undefined,
	scale: number | null,
	onPlace?: (place: ImageViewportPlace) => void,
): void {
	if (!el) return;
	el.scrollLeft = 0;
	el.scrollTop = 0;
	onPlace?.({ scrollLeft: el.scrollLeft, scrollTop: el.scrollTop, scale });
}

/** Coalesce native scroll and pointer-pan updates into one place sample per animation frame. */
export function observeImageViewportPlace(
	el: HTMLElement,
	onPlace: (place: ImageViewportPlace) => void,
	getScale: () => number | null,
): () => void {
	let frame = 0;
	const record = () => {
		frame = 0;
		onPlace({ scrollLeft: el.scrollLeft, scrollTop: el.scrollTop, scale: getScale() });
	};
	const onScroll = () => { if (!frame) frame = requestAnimationFrame(record); };
	el.addEventListener('scroll', onScroll, { passive: true });
	return () => {
		el.removeEventListener('scroll', onScroll);
		if (frame) cancelAnimationFrame(frame);
	};
}
