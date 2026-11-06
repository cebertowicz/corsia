/** How long after a press a card opening still counts as that click's. */
const CLICK_WINDOW_MS = 800;

const pointerAnchors = new WeakSet<object>();

/** True for an anchor `point()` made, so a card knows it was opened by a click and sits at the pointer. */
export function isPointerAnchor(anchor: unknown): boolean {
	return typeof anchor === 'function' && pointerAnchors.has(anchor);
}

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

/**
 * Remembers the last press in a document so a card it opens can sit at the pointer. `listen` starts the tracking and returns its cleanup;
 * `point` is the box to place the card against, or null for a touch, a keyboard or list opening, a stale press, or the phone's sheet.
 */
export function createClickPoint(root: () => HTMLElement | null | undefined, sheet: () => boolean) {
	let pointer: { x: number; y: number; at: number; type: string; target: EventTarget | null } | null = null;
	return {
		listen() {
			const down = (event: PointerEvent) => { pointer = { x: event.clientX, y: event.clientY, at: Date.now(), type: event.pointerType, target: event.target }; };
			const up = () => { if (pointer) pointer.at = Date.now(); };
			window.addEventListener('pointerdown', down, true);
			window.addEventListener('pointerup', up, true);
			return () => { window.removeEventListener('pointerdown', down, true); window.removeEventListener('pointerup', up, true); };
		},
		point(): (() => DOMRect) | null {
			const seen = pointer;
			if (!seen || seen.type === 'touch' || sheet() || Date.now() - seen.at > CLICK_WINDOW_MS || !(seen.target instanceof Node) || !root()?.contains(seen.target)) return null;
			const line = clickedLine(seen.target, seen.y);
			const rect = new DOMRect(seen.x, line?.top ?? seen.y, 0, line ? line.bottom - line.top : 0);
			const anchor = () => rect;
			pointerAnchors.add(anchor);
			return anchor;
		},
	};
}
