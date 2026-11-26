import { afterEach, describe, expect, it, vi } from 'vitest';
import { fitMiddleEllipsis, fitPrefix } from './middleEllipsis';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.replaceChildren(); });

// A monospace stand-in for the browser's measure: every character, "…" included, is 10 wide.
const wide = (text: string) => Array.from(text).length * 10;

describe('fitMiddleEllipsis', () => {
	function chip(initialWidth: number, head = 'north-', tail = 'head-map.png', charWidth = 10) {
		const measure = (text: string) => Array.from(text).length * charWidth;
		const list = document.createElement('ul');
		const start = document.createElement('span');
		const end = document.createElement('span');
		start.className = 'name-start'; end.className = 'name-end';
		start.textContent = head; end.textContent = tail;
		list.append(start, end); document.body.append(list);
		let width = initialWidth;
		vi.spyOn(list, 'getBoundingClientRect').mockImplementation(() => ({ width }) as DOMRect);
		let resize: () => void = () => {};
		vi.stubGlobal('ResizeObserver', class { constructor(cb: () => void) { resize = cb; } observe() {} disconnect() {} });
		vi.spyOn(document, 'createRange').mockReturnValue({
			selectNodeContents() {}, getBoundingClientRect: () => ({ width: measure(start.textContent ?? '') }),
		} as unknown as Range);
		const available = () => Math.max(0, width - measure(end.textContent ?? ''));
		vi.spyOn(start, 'getBoundingClientRect').mockImplementation(() => ({ width: Math.min(measure(start.textContent ?? ''), available()) }) as DOMRect);
		Object.defineProperty(start, 'clientWidth', { get: () => Math.min(measure(start.textContent ?? ''), available()) });
		Object.defineProperty(start, 'scrollWidth', { get: () => measure(start.textContent ?? '') });
		const action = fitMiddleEllipsis(start, { head, tail });
		return { start, end, action, resize: (next: number) => { width = next; resize(); }, destroy: () => action.destroy?.() };
	}

	it('keeps the ellipsis whole and preserves the extension at a constrained width, then restores the suffix on resize', () => {
		const c = chip(125);
		expect(c.start.scrollWidth).toBeLessThanOrEqual(c.start.clientWidth);
		expect(c.start.textContent).toBe('nort…');
		expect(c.end.textContent).toBe('map.png');
		c.resize(145);
		expect(c.start.textContent).toBe('north…');
		expect(c.end.textContent).toBe('-map.png');
		c.resize(200);
		expect(c.start.textContent).toBe('north-');
		expect(c.end.textContent).toBe('head-map.png');
		c.destroy();
	});

	it('keeps the start and extension at the measured 320px title width', () => {
		// 101.390625 CSS px measured on the image chip; 7.5px characters approximate its rendered tail.
		const c = chip(101.390625, 'north-trail', 'head-map.png', 7.5);
		expect(c.start.textContent).toMatch(/^.+…$/);
		expect(c.end.textContent).toMatch(/\.png$/);
		expect((c.start.textContent?.length ?? 0) - 1).toBeGreaterThanOrEqual((c.end.textContent?.length ?? 0) - 4);
		expect(c.start.scrollWidth).toBeLessThanOrEqual(c.start.clientWidth);
		c.destroy();
	});

	it('keeps only an ellipsis and extension when no start character fits', () => {
		const c = chip(37.5, 'north-trail', 'head-map.png', 7.5);
		expect(c.start.textContent).toBe('…');
		expect(c.end.textContent).toBe('.png');
		c.destroy();
	});

	it('keeps the approved 393px long-name cuts when they already fit', () => {
		const pdf = chip(300, 'north-trailhead-lot-and-other-details-', 'final-v7.pdf');
		expect(pdf.start.textContent + pdf.end.textContent).toBe('north-trailhead-l…final-v7.pdf');
		pdf.destroy();
		const png = chip(260, 'drainage_cut_and_more_detail_', 'unbroken.png');
		expect(png.start.textContent + png.end.textContent).toBe('drainage_cut_…unbroken.png');
		png.destroy();
	});

	it('uses the new name on update without replacing the tail text node', () => {
		const c = chip(200);
		const tailText = c.end.firstChild;
		c.action.update?.({ head: 'garden-', tail: 'list.pdf' });
		expect(c.start.textContent).toBe('garden-');
		expect(c.end.textContent).toBe('list.pdf');
		expect(c.end.firstChild).toBe(tailText);
		c.destroy();
	});

	it('keeps the complete name when the chip has no layout', () => {
		const c = chip(0);
		expect(c.start.textContent).toBe('north-');
		expect(c.end.textContent).toBe('head-map.png');
		c.destroy();
	});

	it('keeps a roomy chip byte-identical', () => {
		const c = chip(200);
		expect(c.start.textContent).toBe('north-');
		expect(c.end.textContent).toBe('head-map.png');
		c.destroy();
	});
});

describe('fitPrefix', () => {
	it('returns a start that already fits as it is, with no ellipsis', () => {
		expect(fitPrefix('drainage', wide, 80)).toBe('drainage');
		expect(fitPrefix('drainage', wide, 200)).toBe('drainage');
	});

	it('cuts to the longest start that fits with the ellipsis, leaving no spare room', () => {
		const out = fitPrefix('drainage_cut_and_bridge', wide, 100);
		expect(out).toBe('drainage_…');
		expect(wide(out)).toBe(100);
	});

	it('uses a fractional width without going over it', () => {
		const out = fitPrefix('drainage_cut_and_bridge', wide, 104.9);
		expect(out).toBe('drainage_…');
		expect(wide(out)).toBeLessThanOrEqual(104.9);
	});

	it('keeps whole characters, never half of a pair', () => {
		expect(fitPrefix('😀😀😀😀😀', wide, 30)).toBe('😀😀…');
	});

	it('falls to the bare ellipsis when not even one letter fits', () => {
		expect(fitPrefix('drainage', wide, 10)).toBe('…');
		expect(fitPrefix('drainage', wide, 5)).toBe('…');
	});
});
