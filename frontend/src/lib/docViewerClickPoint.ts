import { get } from 'svelte/store';
import { isMobile } from '$lib/stores/viewport';
import type { createCommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';
import type { createCommentScrollTarget } from '$lib/docViewerScrollTarget.svelte';

/** The line box under the click: the text rect at the click's height, widened by the line's half-leading. A margin marker's own box stands in for its line. */
function clickedLine(target: Node, y: number): { top: number; bottom: number } | null {
	const el = target instanceof Element ? target : target.parentElement;
	const marker = el?.closest('.cm-comment-marker');
	if (marker) { const box = marker.getBoundingClientRect(); return { top: box.top, bottom: box.bottom }; }
	if (!el) return null;
	const range = document.createRange();
	range.selectNodeContents(el);
	const text = [...range.getClientRects()].filter((rect) => rect.height > 0 && rect.top <= y + 1 && rect.bottom >= y - 1).sort((a, b) => a.height - b.height)[0];
	if (!text) return null;
	const leading = Math.max(0, (parseFloat(getComputedStyle(el).lineHeight) - text.height) / 2) || 0;
	return { top: text.top - leading, bottom: text.bottom + leading };
}

// A click in the text or on a margin marker opens its card at the pointer, and scrolls nothing (every width but a phone's, which keeps its sheet).
const clicked = (click: MouseEvent | undefined, live: () => DOMRect | null) => get(isMobile) ? null : clickAnchor(click, live);

/** Opens a comment's card, or a margin marker's cards: at the click that asked for them, else beside their text, scrolled to. */
export function createCommentOpeners(ui: ReturnType<typeof createCommentSurfaceState>, scroll: ReturnType<typeof createCommentScrollTarget>) {
	return {
		comment(id: string, anchor: () => DOMRect | null, viaSuggestion?: boolean, click?: MouseEvent) {
			const point = clicked(click, anchor);
			if (!ui.focusComment(id, point ?? anchor, viaSuggestion, !!point)) return;
			if (point) scroll.stay(id); else scroll.toComment(id, false, 'navigation');
		},
		markers(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null, click?: MouseEvent) {
			const point = clicked(click, reading ?? (() => (opener.isConnected ? opener.getBoundingClientRect() : null)));
			if (point) scroll.stay(ids[0]); else if (ids.length === 1) scroll.toComment(ids[0], false, 'navigation');
			ui.focusMarker(ids, opener, point ?? reading, !!point);
		},
	};
}

/**
 * The box a card opened by `click` sits against: the clicked line at the pointer's x. Null when no click opened it, for a
 * keyboard press on a button (a click with no pointer, `detail` 0), so the card opens beside the comment's text as before.
 * The box keeps its offset from `live` (the comment's own box), so the card follows its text as it scrolls or moves and
 * hides when `live` does.
 */
export function clickAnchor(click: MouseEvent | undefined, live: () => DOMRect | null): (() => DOMRect | null) | null {
	if (!click || click.detail === 0 || !(click.target instanceof Node)) return null;
	const line = clickedLine(click.target, click.clientY);
	const rect = new DOMRect(click.clientX, line?.top ?? click.clientY, 0, line ? line.bottom - line.top : 0);
	const from = live();
	if (!from) return () => rect;
	return () => {
		const now = live();
		return now && new DOMRect(rect.x + now.left - from.left, rect.y + now.top - from.top, 0, rect.height);
	};
}
