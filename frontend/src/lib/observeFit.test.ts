import { afterEach, describe, expect, it, vi } from 'vitest';
import { observeFit } from './observeFit';

const sized = (width: number) => ({ getBoundingClientRect: () => ({ width }) }) as unknown as HTMLElement;

describe('observeFit', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('reports whether the content fits the box, straight away', () => {
		const seen: boolean[] = [];
		observeFit(sized(100), sized(60), (fits) => seen.push(fits));
		observeFit(sized(40), sized(60), (fits) => seen.push(fits));
		expect(seen).toEqual([true, false]);
	});

	it('re-reports on resize and stops when released', () => {
		let resize = () => {};
		const disconnect = vi.fn();
		vi.stubGlobal('ResizeObserver', class { constructor(cb: () => void) { resize = cb; } observe() {} disconnect = disconnect; });
		let width = 100;
		const box = { getBoundingClientRect: () => ({ width }) } as unknown as HTMLElement;
		const seen: boolean[] = [];
		const stop = observeFit(box, sized(60), (fits) => seen.push(fits));
		width = 40;
		resize();
		expect(seen).toEqual([true, false]);
		stop();
		expect(disconnect).toHaveBeenCalled();
	});
});
