import { clippedWindow } from '$lib/cm/visible-region';

/**
 * Whether `box` takes room inside what the person sees now: the visual viewport (a phone's keyboard and pinch zoom
 * shrink it) and the part every clipping ancestor shows (`clippedWindow`), so a box its scroller has scrolled away is not
 * on screen. Nor is a box drawn invisible where it lies: hidden (`visibility`, which it inherits) or inside an inert
 * surface (a Popover not yet placed is inert as well as clipped away).
 */
export function elementOnScreen(box: Element): boolean {
	if (getComputedStyle(box).visibility !== 'visible' || box.closest('[inert]')) return false;
	const rect = box.getBoundingClientRect(), view = window.visualViewport, { rect: clip } = clippedWindow(box);
	const left = Math.max(rect.left, view?.offsetLeft ?? 0, clip.left);
	const top = Math.max(rect.top, view?.offsetTop ?? 0, clip.top);
	const right = Math.min(rect.right, (view?.offsetLeft ?? 0) + (view?.width ?? window.innerWidth), clip.right);
	const bottom = Math.min(rect.bottom, (view?.offsetTop ?? 0) + (view?.height ?? window.innerHeight), clip.bottom);
	return right > left && bottom > top;
}
