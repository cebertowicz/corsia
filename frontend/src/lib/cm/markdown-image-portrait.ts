import { CELL_EDITOR } from './widget-cell-state';

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
