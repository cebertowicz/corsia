import { describe, expect, it } from 'vitest';
import { COARSE_RAIL, FINE_RAIL, markerRailWidth, readPointerEnvironment } from './comment-clusters';

describe('markerRailWidth', () => {
	it('is 24px for a fine pointer with no touch', () => {
		expect(markerRailWidth({ coarse: false, touchPoints: 0, touchEvents: false })).toBe(FINE_RAIL);
	});

	it('is 44px when any pointer is coarse', () => {
		expect(markerRailWidth({ coarse: true, touchPoints: 0, touchEvents: false })).toBe(COARSE_RAIL);
	});

	it('is 44px when touch is present even though no pointer reports coarse', () => {
		expect(markerRailWidth({ coarse: false, touchPoints: 5, touchEvents: false })).toBe(COARSE_RAIL);
		expect(markerRailWidth({ coarse: false, touchPoints: 0, touchEvents: true })).toBe(COARSE_RAIL);
	});

	it('is 44px when nothing about the pointer is known', () => {
		expect(markerRailWidth({})).toBe(COARSE_RAIL);
	});

	it('stays 24px when only the touch count is unknown but the media query says fine', () => {
		expect(markerRailWidth({ coarse: false })).toBe(FINE_RAIL);
	});

	it('reads the environment from the window it is given', () => {
		const win = { matchMedia: (query: string) => ({ matches: query === '(any-pointer: coarse)' }), navigator: { maxTouchPoints: 0 } } as unknown as Window;
		expect(readPointerEnvironment(win)).toEqual({ coarse: true, touchPoints: 0, touchEvents: false });
		expect(readPointerEnvironment({ navigator: {} } as unknown as Window)).toEqual({ coarse: undefined, touchPoints: undefined, touchEvents: false });
	});
});
