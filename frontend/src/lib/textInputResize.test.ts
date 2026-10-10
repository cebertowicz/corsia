import { describe, expect, it } from 'vitest';
import { resizeTextInputToContent } from './textInputResize';

function entryWithScrollHeight(scrollHeight: number): HTMLElement {
	const element = document.createElement('div');
	Object.defineProperty(element, 'scrollHeight', { configurable: true, value: scrollHeight });
	return element;
}

describe('resizeTextInputToContent', () => {
	it('grows to the content height without scrolling below the limit', () => {
		const element = entryWithScrollHeight(72);

		expect(resizeTextInputToContent(element, 150)).toBe(72);
		expect(element.style.height).toBe('72px');
		expect(element.style.overflowY).toBe('hidden');
	});

	it('caps the height and enables scrolling above the limit', () => {
		const element = entryWithScrollHeight(240);

		expect(resizeTextInputToContent(element, 150)).toBe(150);
		expect(element.style.height).toBe('150px');
		expect(element.style.overflowY).toBe('auto');
	});
});
