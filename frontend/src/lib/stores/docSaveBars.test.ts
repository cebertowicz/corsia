import { afterEach, describe, expect, it, vi } from 'vitest';
import { docSaveBars, showsSaveBar } from './docSaveBars';

function bar(rect: Partial<DOMRect> = {}): Element {
	const element = document.createElement('div');
	element.getBoundingClientRect = () => ({ left: 10, top: 10, right: 410, bottom: 310, width: 400, height: 300, x: 10, y: 10, toJSON: () => ({}), ...rect }) as DOMRect;
	return element;
}

afterEach(() => {
	docSaveBars.reset();
	vi.unstubAllGlobals();
});

describe('docSaveBars', () => {
	it('reads a mounted bar as on screen only while it takes room inside the window', () => {
		const drop = docSaveBars.shown('tab', bar());
		expect(docSaveBars.onScreen('tab')).toBe(true);
		expect(docSaveBars.onScreen('other tab')).toBe(false);
		expect(docSaveBars.onScreen('')).toBe(false);
		drop();
		expect(docSaveBars.onScreen('tab')).toBe(false);
	});

	it('does not count a bar that is mounted but hidden: no room, or outside the window', () => {
		docSaveBars.shown('hidden', bar({ width: 0, height: 0, right: 10, bottom: 10 }));
		docSaveBars.shown('below', bar({ top: 5000, bottom: 5300 }));
		docSaveBars.shown('right', bar({ left: 5000, right: 5400 }));
		expect(docSaveBars.onScreen('hidden')).toBe(false);
		expect(docSaveBars.onScreen('below')).toBe(false);
		expect(docSaveBars.onScreen('right')).toBe(false);
	});

	it('follows the visual viewport, which a phone’s keyboard shrinks', () => {
		docSaveBars.shown('tab', bar({ top: 500, bottom: 700 }));
		expect(docSaveBars.onScreen('tab')).toBe(true);
		vi.stubGlobal('visualViewport', { offsetLeft: 0, offsetTop: 0, width: 400, height: 300 });
		expect(docSaveBars.onScreen('tab')).toBe(false);
	});

	it('counts the tab as on screen while any of its bars is', () => {
		docSaveBars.shown('tab', bar({ width: 0, height: 0 }));
		const drop = docSaveBars.shown('tab', bar());
		expect(docSaveBars.onScreen('tab')).toBe(true);
		drop();
		expect(docSaveBars.onScreen('tab')).toBe(false);
	});

	it('registers an element under its key while the action is mounted, and moves with a new key', () => {
		const node = bar();
		const action = showsSaveBar(node, 'one');
		expect(docSaveBars.onScreen('one')).toBe(true);
		action.update('two');
		expect(docSaveBars.onScreen('one')).toBe(false);
		expect(docSaveBars.onScreen('two')).toBe(true);
		action.destroy();
		expect(docSaveBars.onScreen('two')).toBe(false);
	});
});
