import { describe, expect, it } from 'vitest';
import { getSlideWidth, getPaneWidth, trackDrag, shouldOpenOnRelease, releaseVelocity, OVER_OPEN_RUBBER, FLICK_VELOCITY, VELOCITY_STALE_MS } from '../drawerDrag';

describe('drawerDrag — reveal widths', () => {
	it('sidebar = min(320, 85vw)', () => {
		expect(getSlideWidth(375)).toBeCloseTo(318.75); // narrow phone → 85vw
		expect(getSlideWidth(1440)).toBe(320); // desktop → capped at 320
	});
	it('pane = min(420, 92vw)', () => {
		expect(getPaneWidth(375)).toBe(345); // narrow phone → 92vw
		expect(getPaneWidth(1440)).toBe(420); // wide → capped at 420
	});
});

describe('drawerDrag — finger tracking (rubber-band at the open edge)', () => {
	const slide = 320;
	const pane = 420;

	it('sidebar: 1:1 in range, hard floor at 0, rubber past full open', () => {
		expect(trackDrag('sidebar', -50, slide, pane)).toBe(0); // over-close floors
		expect(trackDrag('sidebar', 100, slide, pane)).toBe(100); // tracks finger 1:1
		expect(trackDrag('sidebar', slide + 100, slide, pane)).toBeCloseTo(slide + 100 * OVER_OPEN_RUBBER);
	});

	it('pane: 1:1 in range, hard floor at 0, rubber past full open', () => {
		expect(trackDrag('pane', 50, slide, pane)).toBe(0); // over-close floors
		expect(trackDrag('pane', -100, slide, pane)).toBe(-100); // tracks finger 1:1
		expect(trackDrag('pane', -pane - 100, slide, pane)).toBeCloseTo(-pane - 100 * OVER_OPEN_RUBBER);
	});
});

describe('drawerDrag — flick velocity overrides position', () => {
	const slide = 320;
	const pane = 420;

	it('a rightward flick opens the sidebar from anywhere', () => {
		expect(shouldOpenOnRelease('sidebar', 30, slide, pane, FLICK_VELOCITY)).toBe(true);
	});
	it('a leftward flick closes the sidebar even when nearly open', () => {
		expect(shouldOpenOnRelease('sidebar', 300, slide, pane, -FLICK_VELOCITY)).toBe(false);
	});
	it('a leftward flick opens the pane from anywhere', () => {
		expect(shouldOpenOnRelease('pane', -30, slide, pane, -FLICK_VELOCITY)).toBe(true);
	});
	it('a rightward flick closes the pane even when nearly open', () => {
		expect(shouldOpenOnRelease('pane', -400, slide, pane, FLICK_VELOCITY)).toBe(false);
	});
	it('sub-flick velocity falls back to the 40% positional snap', () => {
		expect(shouldOpenOnRelease('sidebar', 129, slide, pane, 0.2)).toBe(true);
		expect(shouldOpenOnRelease('sidebar', 100, slide, pane, 0.2)).toBe(false);
	});
});

describe('drawerDrag — 40% snap threshold on release', () => {
	const slide = 320; // 40% = 128
	const pane = 420; // 40% = 168

	it('sidebar opens only past +40% of the slide width', () => {
		expect(shouldOpenOnRelease('sidebar', 129, slide, pane)).toBe(true);
		expect(shouldOpenOnRelease('sidebar', 128, slide, pane)).toBe(false);
		expect(shouldOpenOnRelease('sidebar', 0, slide, pane)).toBe(false);
	});

	it('pane opens only past -40% of the pane width (leftward drag)', () => {
		expect(shouldOpenOnRelease('pane', -169, slide, pane)).toBe(true);
		expect(shouldOpenOnRelease('pane', -168, slide, pane)).toBe(false);
		expect(shouldOpenOnRelease('pane', 0, slide, pane)).toBe(false);
		expect(shouldOpenOnRelease('pane', 200, slide, pane)).toBe(false);
	});
});

describe('drawerDrag — release velocity from move samples', () => {
	it('computes px/ms from the last two samples', () => {
		expect(releaseVelocity(100, 1000, 160, 1040, 1040)).toBeCloseTo(1.5);
		expect(releaseVelocity(160, 1000, 100, 1040, 1040)).toBeCloseTo(-1.5);
	});
	it('zeroes when the last sample is stale (finger paused before release)', () => {
		expect(releaseVelocity(100, 1000, 160, 1040, 1040 + VELOCITY_STALE_MS + 1)).toBe(0);
	});
	it('still flicks when released just inside the staleness window', () => {
		expect(releaseVelocity(100, 1000, 160, 1040, 1040 + VELOCITY_STALE_MS)).toBeCloseTo(1.5);
	});
	it('zeroes on degenerate samples (dt ≤ 0)', () => {
		expect(releaseVelocity(100, 1040, 160, 1040, 1040)).toBe(0);
		expect(releaseVelocity(100, 1050, 160, 1040, 1060)).toBe(0);
	});
});
