import { mount, unmount } from 'svelte';
import NewMarker from './NewMarker.svelte';

/** What the pinned sign says is new, as its accessible name. */
export type NewSignName = 'New comment' | 'New reply';

/** Pin the new sign over the end of a highlight, out of text flow so no line moves for it, kept visible within the scroller. */
export function placeHighlightDot(body: HTMLElement, anchor: HTMLElement, failedQuote: string | null = null, onVisibility?: (visible: boolean) => void, name: NewSignName = 'New reply'): () => void {
	const host = document.createElement('span');
	const layer = body.parentElement?.classList.contains('comment-mark-layer') ? body.parentElement : null;
	host.dataset.highlightDot = '';
	host.style.cssText = `position:${layer ? 'absolute' : 'fixed'};z-index:5;pointer-events:none;display:none;line-height:0`;
	(layer ?? document.body).append(host);
	const marker = mount(NewMarker, { target: host, props: { pinned: true, failedQuote, name } });
	let frame = 0;
	function update() {
		frame = 0;
		const setVisible = (visible: boolean) => { host.style.display = visible ? 'inline-flex' : 'none'; onVisibility?.(visible); };
		const textRange = document.createRange();
		textRange.selectNodeContents(anchor);
		const rects = anchor.matches('td, th') ? [...textRange.getClientRects()] : [...anchor.getClientRects()];
		const last = rects.at(-1);
		// Its own size: the word, or a failed reply's dot.
		host.style.display = 'inline-flex';
		const width = host.offsetWidth, height = host.offsetHeight;
		if (!last || width <= 0 || height <= 0) { setVisible(false); return; }
		const scroller = body.closest<HTMLElement>('.messages, .cm-scroller');
		const clip = scroller?.getBoundingClientRect();
		const headerBottom = document.querySelector('.shell-header')?.getBoundingClientRect().bottom ?? 0;
		const top = Math.max(0, headerBottom, clip?.top ?? 0), bottom = Math.min(innerHeight, clip?.bottom ?? innerHeight);
		if (last.right < 0 || last.left > innerWidth || last.bottom < top || last.top > bottom || bottom - top < height) { setVisible(false); return; }
		const x = Math.max(0, Math.min(innerWidth - width, last.right - width));
		const y = Math.max(top, Math.min(bottom - height, last.top - height - 1));
		const layerRect = layer?.getBoundingClientRect();
		host.style.left = `${x - (layerRect?.left ?? 0)}px`; host.style.top = `${y - (layerRect?.top ?? 0)}px`; setVisible(true);
	}
	const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
	const observer = new ResizeObserver(schedule);
	observer.observe(body);
	// Mobile keyboard focus changes the document's message lead without resizing the body.
	const layoutObserver = new MutationObserver(schedule);
	layoutObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['style'] });
	window.addEventListener('scroll', schedule, true);
	window.addEventListener('resize', schedule);
	update();
	return () => { if (frame) cancelAnimationFrame(frame); observer.disconnect(); layoutObserver.disconnect(); window.removeEventListener('scroll', schedule, true); window.removeEventListener('resize', schedule); void unmount(marker); host.remove(); onVisibility?.(false); };
}
