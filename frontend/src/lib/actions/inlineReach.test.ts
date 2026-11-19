import { afterEach, describe, expect, it, vi } from 'vitest';
import { inlineReach } from './inlineReach';

const observed: { target: Element; options?: ResizeObserverOptions }[] = [];
let callbacks: (() => void)[] = [];
class FakeObserver {
	constructor(private callback: () => void) { callbacks.push(callback); }
	observe(target: Element, options?: ResizeObserverOptions) { observed.push({ target, options }); }
	disconnect() { callbacks = callbacks.filter((callback) => callback !== this.callback); }
}

function box(element: HTMLElement, rect: { left: number; right: number }) {
	element.getBoundingClientRect = () => ({ ...rect, top: 0, bottom: 0, width: rect.right - rect.left, height: 0, x: rect.left, y: 0, toJSON: () => ({}) });
}

afterEach(() => { observed.length = 0; callbacks = []; vi.unstubAllGlobals(); });

describe('inlineReach', () => {
	it('reports how far the controls reach in from the shell end, now and on every resize, and waits for its shell', () => {
		vi.stubGlobal('ResizeObserver', FakeObserver);
		const shell = document.createElement('header'), controls = document.createElement('div');
		box(shell, { left: 0, right: 1000 }); box(controls, { left: 879.4, right: 990 });
		const report = vi.fn();
		const action = inlineReach(controls, { shell: null, report });
		expect(report).not.toHaveBeenCalled();
		action.update({ shell, report });
		expect(report).toHaveBeenLastCalledWith(121);
		expect(observed.find((entry) => entry.target === controls)?.options).toEqual({ box: 'border-box' });
		box(controls, { left: 800, right: 990 });
		callbacks.forEach((callback) => callback());
		expect(report).toHaveBeenLastCalledWith(200);
		action.destroy();
		expect(callbacks).toHaveLength(0);
	});
});
