import { mount, unmount } from 'svelte';
import NewMarker from './NewMarker.svelte';

/** PROTOTYPE (uncommitted): which placement to draw. 'before' is origin/dev. */
export const proto: { variant: 'before' | 'gutter' | 'under' | 'trailing' | 'tint' } = { variant: 'before' };

/** What the pinned sign says is new, as its accessible name. */
export type NewSignName = 'New comment' | 'New reply';

/** Pin the new sign over the end of a highlight, out of text flow so no line moves for it, kept visible within the scroller. */
export function placeHighlightDot(body: HTMLElement, anchor: HTMLElement, failedQuote: string | null = null, onVisibility?: (visible: boolean) => void, name: NewSignName = 'New reply'): () => void {
	const host = document.createElement('span');
	const layer = body.parentElement?.classList.contains('comment-mark-layer') ? body.parentElement : null;
	host.dataset.highlightDot = '';
	host.style.cssText = `position:${layer ? 'absolute' : 'fixed'};z-index:5;pointer-events:none;display:none;line-height:0`;
	(layer ?? document.body).append(host);
	const wordHost = document.createElement('span'), dotHost = document.createElement('span');
	wordHost.style.cssText = 'display:inline-flex'; dotHost.style.cssText = 'display:none';
	host.append(wordHost, dotHost);
	const marker = mount(NewMarker, { target: wordHost, props: { pinned: true, failedQuote, name } });
	const dotMarker = mount(NewMarker, { target: dotHost, props: { pinned: true, failedQuote, name, mark: 'dot' } });
	const textRects = () => {
		const out: DOMRect[] = [];
		const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
		for (let node = walker.nextNode(); node; node = walker.nextNode()) {
			if (!(node.textContent ?? '').trim()) continue;
			const range = document.createRange(); range.selectNodeContents(node);
			out.push(...range.getClientRects());
		}
		return out;
	};
	let frame = 0;
	function update() {
		frame = 0;
		const setVisible = (visible: boolean) => { host.style.display = visible ? 'inline-flex' : 'none'; onVisibility?.(visible); };
		const textRange = document.createRange();
		textRange.selectNodeContents(anchor);
		const rects = anchor.matches('td, th') ? [...textRange.getClientRects()] : [...anchor.getClientRects()];
		const last = rects.at(-1);
		// Its own size: the word, or a failed reply's dot.
		const variant = proto.variant;
		const showDot = (on: boolean) => { dotHost.style.display = on ? 'inline-flex' : 'none'; wordHost.style.display = on ? 'none' : 'inline-flex'; };
		showDot(false);
		host.style.display = 'inline-flex';
		let width = host.offsetWidth, height = host.offsetHeight;
		let mode: 'above' | 'gutter' | 'under' | 'trailing' | 'tint' = 'above';
		const bodyRect = body.getBoundingClientRect();
		const scrollerEl = body.closest<HTMLElement>('.messages, .cm-scroller');
		if (variant === 'gutter' && last) {
			const room = (scrollerEl?.getBoundingClientRect().right ?? innerWidth) - bodyRect.right;
			mode = width + 8 <= room ? 'gutter' : 'tint';
		} else if (variant === 'tint') mode = 'tint';
		else if (variant === 'under') mode = 'under';
		else if (variant === 'trailing' && last) {
			const gap = 4, x0 = last.right + gap, x1 = x0 + width;
			const clear = x1 <= bodyRect.right && !textRects().some((r) => r.width > 0.5 && r.left < x1 + 1 && r.right > x0 - 1 && r.top < last.bottom && r.bottom > last.top);
			mode = clear ? 'trailing' : 'tint';
		}
		if (mode === 'under') { showDot(true); width = host.offsetWidth; height = host.offsetHeight; }
		host.dataset.mode = mode;
		// Option D: the highlight itself is the sign. The sign host stays for its accessible name but draws nothing.
		if (mode === 'tint') anchor.setAttribute('data-unread', ''); else anchor.removeAttribute('data-unread');
		if (!last || width <= 0 || height <= 0) { setVisible(false); return; }
		const scroller = body.closest<HTMLElement>('.messages, .cm-scroller');
		const clip = scroller?.getBoundingClientRect();
		const headerBottom = document.querySelector('.shell-header')?.getBoundingClientRect().bottom ?? 0;
		const top = Math.max(0, headerBottom, clip?.top ?? 0), bottom = Math.min(innerHeight, clip?.bottom ?? innerHeight);
		if (last.right < 0 || last.left > innerWidth || last.bottom < top || last.top > bottom || bottom - top < height) { setVisible(false); return; }
		let x = Math.max(0, Math.min(innerWidth - width, last.right - width));
		let y = Math.max(top, Math.min(bottom - height, last.top - height - 1));
		if (mode === 'gutter') { x = bodyRect.right + 8; y = last.top + (last.height - height) / 2; }
		else if (mode === 'under') {
			// The dot's right edge meets the highlight's, 1px below its last line box. Hidden when any part of it, its 1px ring included, would pass the scroller's clip.
			x = Math.max(0, last.right - width); y = last.bottom + 1;
			host.dataset.anchor = `${anchor.tagName.toLowerCase()}${anchor.className ? `.${String(anchor.className).trim().split(/\s+/).join('.')}` : ''}`;
			host.dataset.ref = [last.left, last.top, last.right, last.bottom].map((v) => Math.round(v * 10) / 10).join(',');
			if (y - 1 < top || y + height + 1 > bottom) { setVisible(false); return; }
		}
		else if (mode === 'trailing') { x = last.right + 4; y = last.top + (last.height - height) / 2; }
		else if (mode === 'tint') { x = last.right; y = last.top; host.style.opacity = '0'; }
		if (mode !== 'tint') host.style.opacity = '';
		// A word in the gutter or the trailing space is hidden when any part of it would pass the scroller's clip.
		if ((mode === 'gutter' || mode === 'trailing') && (y < top || y + height > bottom)) { setVisible(false); return; }
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
	return () => { anchor.removeAttribute('data-unread'); if (frame) cancelAnimationFrame(frame); observer.disconnect(); layoutObserver.disconnect(); window.removeEventListener('scroll', schedule, true); window.removeEventListener('resize', schedule); void unmount(marker); void unmount(dotMarker); host.remove(); onVisibility?.(false); };
}
