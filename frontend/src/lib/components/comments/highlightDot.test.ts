import { afterEach, expect, it, vi } from 'vitest';
import { placeHighlightDot } from './highlightDot';

afterEach(() => { document.body.replaceChildren(); Reflect.deleteProperty(Range.prototype, 'getClientRects'); vi.restoreAllMocks(); });

it('pins the New sign over the highlight\'s end, out of text flow, even when the previous line leaves only a narrow band', () => {
	vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(28);
	vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(5);
	const scroller = document.createElement('div');
	scroller.className = 'messages';
	scroller.innerHTML = '<div>previous <span>highlight</span></div>';
	document.body.append(scroller);
	const body = scroller.firstElementChild as HTMLElement;
	const anchor = body.querySelector('span')!;
	vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue({ top: 0, bottom: 400 } as DOMRect);
	vi.spyOn(anchor, 'getClientRects').mockReturnValue([{ left: 20, right: 80, top: 100, bottom: 120 }] as unknown as DOMRectList);
	const rangeRects = vi.fn(() => [{ top: 75, bottom: 94.1 }]);
	Object.defineProperty(Range.prototype, 'getClientRects', { value: rangeRects, configurable: true });
	const cleanup = placeHighlightDot(body, anchor);
	const marker = document.querySelector<HTMLElement>('[data-highlight-dot]')!;
	expect(marker.style.display).toBe('inline-flex');
	expect(marker.style.position).toBe('fixed');
	expect(marker.querySelector('.ct-new-dot'), 'the dot is gone').toBeNull();
	expect(marker.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('New reply');
	expect(marker.textContent).toBe('New');
	expect(marker.style.left).toBe('52px');
	cleanup();
	Reflect.deleteProperty(Range.prototype, 'getClientRects');
});
