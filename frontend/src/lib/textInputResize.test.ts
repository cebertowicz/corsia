import { readFileSync } from 'node:fs';
import { join } from 'node:path';
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

	it('measures with transitions off and restores the height on show before the new one', () => {
		const element = entryWithScrollHeight(96);
		element.style.height = '40px';
		element.style.transition = 'height 120ms';
		element.getBoundingClientRect = () => ({ height: 40 }) as DOMRect;
		const heights: string[] = [];
		const transitions: string[] = [];
		const style = element.style;
		Object.defineProperty(element, 'style', {
			value: new Proxy(style, {
				set(target, key, value) {
					if (key === 'height') { heights.push(String(value)); transitions.push(target.transition); }
					(target as unknown as Record<string, string>)[key as string] = value;
					return true;
				},
			}),
		});

		expect(resizeTextInputToContent(element, 150)).toBe(96);
		expect(heights).toEqual(['auto', '40px', '96px']);
		expect(transitions).toEqual(['none', 'none', 'height 120ms']);
		expect(element.style.transition).toBe('height 120ms');
	});

	it('takes the new height at once under reduced motion', () => {
		const css = readFileSync(join(process.cwd(), 'src/lib/components/InputBar.css'), 'utf8');
		const reduced = css.slice(css.lastIndexOf('@media (prefers-reduced-motion: reduce)'));
		expect(reduced).toContain('.input-chip .composer { transition: none; }');
	});
});
