import { CELL_EDITOR } from './widget-cell-state';

/**
 * The height a portrait image is held to is `min(PORTRAIT_CAP_SVH svh, PORTRAIT_CAP_PX px)`; `theme.ts` builds the rule from these two numbers.
 * Basis, a decision for the person to reverse, not a measured limit: 60% of the small viewport height keeps a whole tall image on a phone
 * screen with the document's header and some surrounding text still in view, and 640 px stops it growing past one screenful on a tall desktop window (it binds only
 * above a window about 1067 px high). On a real document of 45 screenshots 21 images are capped and 24 are unchanged.
 */
export const PORTRAIT_CAP_SVH = 60;
export const PORTRAIT_CAP_PX = 640;

/** The class a standalone document image carries once it has loaded and is taller than it is wide; theme.ts caps only those. */
export const PORTRAIT_IMAGE = 'cm-md-image-portrait';

/** Marks `wrap` portrait when its loaded image is taller than wide; a square or landscape image, or one not loaded yet, is left unmarked. */
export function markPortrait(wrap: HTMLElement, img: HTMLImageElement): void {
	wrap.classList.toggle(PORTRAIT_IMAGE, img.naturalHeight > img.naturalWidth && img.naturalWidth > 0);
}

/** Marks the document's own image widget (`.cm-md-image`) portrait once `img` loads. An image in a table cell is never marked, drawn or while its cell is open. */
export function markPortraitOnLoad(wrap: HTMLElement, img: HTMLImageElement): void {
	if (!wrap.classList.contains('cm-md-image')) return;
	img.addEventListener('load', () => { if (!wrap.closest(CELL_EDITOR)) markPortrait(wrap, img); });
}
