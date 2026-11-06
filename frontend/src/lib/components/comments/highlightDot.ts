import { mount, unmount } from 'svelte';
import NewMarker from './NewMarker.svelte';

/** Prototype: keep the shared marker out of the text flow and only show it when the line gap has clearance. */
export function placeHighlightDot(body: HTMLElement, anchor: HTMLElement, failedQuote: string | null = null, onVisibility?: (visible: boolean) => void): () => void {
	const host = document.createElement('span');
	const layer = body.parentElement?.classList.contains('comment-mark-layer') ? body.parentElement : null;
	host.dataset.highlightDot = '';
	host.style.cssText = `position:${layer ? 'absolute' : 'fixed'};z-index:5;pointer-events:none;display:none;line-height:0`;
	(layer ?? document.body).append(host);
	const marker = mount(NewMarker, { target: host, props: { dotOnly: true, failedQuote } });
	let frame = 0;
	function update() {
		frame = 0;
		const setVisible = (visible: boolean) => { host.style.display = visible ? 'inline-flex' : 'none'; onVisibility?.(visible); };
		const textRange = document.createRange();
		textRange.selectNodeContents(anchor);
		const rects = anchor.matches('td, th') ? [...textRange.getClientRects()] : [...anchor.getClientRects()];
		const last = rects.at(-1);
		const size = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--space-1'));
		if (!last || !Number.isFinite(size) || size <= 0) { setVisible(false); return; }
		const x = last.right - size;
		const y = last.top - size - 1;
		const scroller = body.closest<HTMLElement>('.messages, .cm-scroller');
		const clip = scroller?.getBoundingClientRect();
		const headerBottom = document.querySelector('.shell-header')?.getBoundingClientRect().bottom ?? 0;
		if (x < 0 || x + size > innerWidth || y < Math.max(0, headerBottom, clip?.top ?? 0) || y + size > Math.min(innerHeight, clip?.bottom ?? innerHeight)) { setVisible(false); return; }
		// Reserve the whole band between this line and every earlier glyph, not a
		// cutout over selected characters. A taller preceding run can only reduce it.
		let bandTop = Math.max(0, headerBottom, clip?.top ?? 0);
		const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
		for (let text = walker.nextNode() as Text | null; text; text = walker.nextNode() as Text | null) {
			for (let i = 0; i < text.length; i++) {
				if (/\s/.test(text.data[i])) continue;
				const range = document.createRange(); range.setStart(text, i); range.setEnd(text, i + 1);
				for (const glyph of range.getClientRects()) if (glyph.top < last.top - 0.1) bandTop = Math.max(bandTop, glyph.bottom);
			}
		}
		if (last.top - bandTop < size + 2) { setVisible(false); return; }
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
