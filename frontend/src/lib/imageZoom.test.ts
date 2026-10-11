import { describe, expect, it } from 'vitest';
import {
	clampScale, distance, fitScale, fractionOnImage, isActualSize, MAX_SCALE, midpoint, percentLabel,
	scrollToHold, stepScale, wheelFactor,
} from './imageZoom';

describe('fitScale', () => {
	it('shrinks a large image to fit with the margin on every side', () => {
		// Width binds: 500 of room over 1000; then height binds: 152 of room over 500.
		expect(fitScale({ width: 1000, height: 500 }, { width: 548, height: 400 })).toBeCloseTo(0.5, 3);
		expect(fitScale({ width: 1000, height: 500 }, { width: 548, height: 200 })).toBeCloseTo(0.304, 3);
	});

	it('never enlarges a small image past its own size', () => {
		expect(fitScale({ width: 120, height: 80 }, { width: 800, height: 600 })).toBe(1);
	});

	it('stays positive in a viewport smaller than its margins and for an image with no size yet', () => {
		expect(fitScale({ width: 1000, height: 1000 }, { width: 10, height: 10 })).toBeGreaterThan(0);
		expect(fitScale({ width: 0, height: 0 }, { width: 800, height: 600 })).toBe(1);
	});
});

describe('clampScale and stepScale', () => {
	it('keeps zoom between Fit and 8x', () => {
		expect(clampScale(0.01, 0.5)).toBe(0.5);
		expect(clampScale(20, 0.5)).toBe(MAX_SCALE);
		expect(clampScale(2, 0.5)).toBe(2);
	});

	it('steps by a quarter and lands on 100% when a step would cross it', () => {
		expect(stepScale(1, 1, 0.5)).toBeCloseTo(1.25);
		expect(stepScale(0.9, 1, 0.5)).toBe(1);
		expect(stepScale(1.1, -1, 0.5)).toBe(1);
	});

	it('lands on Fit when stepping out would pass it, and does not go below it', () => {
		expect(stepScale(0.55, -1, 0.5)).toBe(0.5);
		expect(stepScale(0.5, -1, 0.5)).toBe(0.5);
	});

	it('does not step past 8x', () => {
		expect(stepScale(7.5, 1, 0.5)).toBe(MAX_SCALE);
		expect(stepScale(MAX_SCALE, 1, 0.5)).toBe(MAX_SCALE);
	});
});

describe('wheelFactor', () => {
	it('zooms in on a negative delta and out on a positive one, and equal opposite deltas cancel', () => {
		expect(wheelFactor(-10)).toBeGreaterThan(1);
		expect(wheelFactor(10)).toBeLessThan(1);
		expect(wheelFactor(10) * wheelFactor(-10)).toBeCloseTo(1);
	});

	it('caps a huge delta and reads line deltas as larger than pixel ones', () => {
		expect(wheelFactor(-5000)).toBe(wheelFactor(-120));
		expect(wheelFactor(-3, 1)).toBeGreaterThan(wheelFactor(-3, 0));
	});
});

describe('pinch and pointer geometry', () => {
	it('measures the distance and midpoint of two touches', () => {
		expect(distance({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5);
		expect(midpoint({ x: 0, y: 10 }, { x: 10, y: 30 })).toEqual({ x: 5, y: 20 });
	});

	it('reads a client point as fractions of the drawn image and clamps outside it', () => {
		const box = { left: 100, top: 50, width: 400, height: 200 };
		expect(fractionOnImage({ x: 300, y: 150 }, box)).toEqual({ x: 0.5, y: 0.5 });
		expect(fractionOnImage({ x: 0, y: 900 }, box)).toEqual({ x: 0, y: 1 });
		expect(fractionOnImage({ x: 1, y: 1 }, { left: 0, top: 0, width: 0, height: 0 })).toEqual({ x: 0.5, y: 0.5 });
	});

	it('scrolls so the point under the pointer stays under it after a resize', () => {
		const at = { x: 0.25, y: 0.5 };
		const pointer = { x: 200, y: 300 };
		// Before zooming the fraction sits exactly under the pointer: nothing to scroll.
		expect(scrollToHold(at, pointer, { left: 100, top: 200, width: 400, height: 200 })).toEqual({ x: 0, y: 0 });
		// After doubling around that point from the same origin the box grew right and down: scroll by the difference.
		const grown = scrollToHold(at, pointer, { left: 100, top: 200, width: 800, height: 400 });
		expect(grown).toEqual({ x: 100, y: 100 });
	});
});

describe('labels', () => {
	it('reports 100% only near 1 and rounds the percentage', () => {
		expect(isActualSize(1)).toBe(true);
		expect(isActualSize(1.004)).toBe(true);
		expect(isActualSize(1.25)).toBe(false);
		expect(percentLabel(0.524)).toBe('52%');
		expect(percentLabel(1)).toBe('100%');
	});
});
