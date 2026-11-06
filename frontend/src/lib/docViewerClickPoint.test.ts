import { afterEach, describe, expect, it, vi } from 'vitest';
import { clickAnchor } from './docViewerClickPoint';

const ownRects = Range.prototype.getClientRects;
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); Range.prototype.getClientRects = ownRects; });

const press = (target: Element, init: MouseEventInit) => {
	const event = new MouseEvent('click', { bubbles: true, ...init });
	target.dispatchEvent(event);
	return event;
};

describe('clickAnchor', () => {
	it('has no point without a click, or for a button pressed from the keyboard', () => {
		const button = document.body.appendChild(document.createElement('button'));
		expect(clickAnchor(undefined)).toBeNull();
		expect(clickAnchor(press(button, { detail: 0, clientX: 5, clientY: 5 }))).toBeNull();
	});
	it('sits at the pointer, against a margin marker’s own box', () => {
		const marker = document.body.appendChild(document.createElement('button'));
		marker.className = 'cm-comment-marker';
		vi.spyOn(marker, 'getBoundingClientRect').mockReturnValue(new DOMRect(4, 100, 20, 18));
		const rect = clickAnchor(press(marker, { detail: 1, clientX: 12, clientY: 108 }))!();
		expect([rect.x, rect.top, rect.width, rect.height]).toEqual([12, 100, 0, 18]);
	});
	it('sits at the pointer, against the clicked line’s box', () => {
		const line = document.body.appendChild(document.createElement('div'));
		line.textContent = 'alpha beta gamma';
		line.style.lineHeight = '24px';
		// jsdom draws no text: give the paragraph two lines of boxes.
		Range.prototype.getClientRects = () => [new DOMRect(0, 200, 300, 20), new DOMRect(0, 224, 300, 20)] as unknown as DOMRectList;
		const rect = clickAnchor(press(line, { detail: 1, clientX: 90, clientY: 230 }))!();
		expect([rect.x, rect.top, rect.height]).toEqual([90, 222, 24]);
	});
});
