import { afterEach, describe, expect, it, vi } from 'vitest';
import { fitMiddleEllipsis, fitPrefix } from './middleEllipsis';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.replaceChildren(); });

// A monospace stand-in for the browser's measure: every character, "…" included, is 10 wide.
const wide = (text: string) => Array.from(text).length * 10;

describe('fitMiddleEllipsis', () => {
	function chip(initialWidth: number) {
		const list = document.createElement('ul');
		const start = document.createElement('span');
		const end = document.createElement('span');
		start.className = 'name-start'; end.className = 'name-end';
		start.textContent = 'north-'; end.textContent = 'head-map.png';
		list.append(start, end); document.body.append(list);
		let width = initialWidth;
		let resize: () => void = () => {};
		vi.stubGlobal('ResizeObserver', class { constructor(cb: () => void) { resize = cb; } observe() {} disconnect() {} });
		vi.spyOn(document, 'createRange').mockReturnValue({
			selectNodeContents() {}, getBoundingClientRect: () => ({ width: wide(start.textContent ?? '') }),
		} as unknown as Range);
		const available = () => Math.max(0, width - wide(end.textContent ?? ''));
		vi.spyOn(start, 'getBoundingClientRect').mockImplementation(() => ({ width: Math.min(wide(start.textContent ?? ''), available()) }) as DOMRect);
		Object.defineProperty(start, 'clientWidth', { get: () => Math.min(wide(start.textContent ?? ''), available()) });
		Object.defineProperty(start, 'scrollWidth', { get: () => wide(start.textContent ?? '') });
		const action = fitMiddleEllipsis(start, { head: 'north-', tail: 'head-map.png' });
		return { start, end, action, resize: (next: number) => { width = next; resize(); }, destroy: () => action.destroy?.() };
	}

	it('keeps the ellipsis whole and preserves the extension at a constrained width, then restores the suffix on resize', () => {
		const c = chip(125);
		expect(c.start.scrollWidth).toBeLessThanOrEqual(c.start.clientWidth);
		expect(c.start.textContent).toBe('…');
		expect(c.end.textContent).toBe('ead-map.png');
		c.resize(145);
		expect(c.start.textContent).toBe('n…');
		expect(c.end.textContent).toBe('head-map.png');
		c.resize(200);
		expect(c.start.textContent).toBe('north-');
		expect(c.end.textContent).toBe('head-map.png');
		c.destroy();
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
