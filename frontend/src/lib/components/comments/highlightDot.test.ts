import { afterEach, expect, it, vi } from 'vitest';
import { placeHighlightDot } from './highlightDot';

afterEach(() => { document.body.replaceChildren(); document.documentElement.style.removeProperty('--space-1'); Reflect.deleteProperty(Range.prototype, 'getClientRects'); vi.restoreAllMocks(); });

it('keeps the unread marker when the previous line leaves only a 5.9px band', () => {
	document.documentElement.style.setProperty('--space-1', '4px');
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
	expect(marker.querySelector('.ct-new-dot')).not.toBeNull();
	cleanup();
	Reflect.deleteProperty(Range.prototype, 'getClientRects');
});
