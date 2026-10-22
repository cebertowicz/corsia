import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DOTS, MOTION, ORIGIN, ORIGINS, SHEEN, createLivingLogo } from './livingLogo.js';

type Played = { target: Element; options: KeyframeAnimationOptions };

let host: HTMLDivElement;
let played: Played[];
const settled = () => ({ finished: Promise.resolve(), cancel() {}, playState: 'running', pause() {}, play() {}, finish() {}, currentTime: 0 });

beforeEach(() => {
	played = [];
	host = document.createElement('div');
	document.body.appendChild(host);
	// jsdom has no Web Animations; record each animation the mark asks for instead.
	Element.prototype.animate = function (this: Element, _keyframes: unknown, options?: KeyframeAnimationOptions) {
		played.push({ target: this, options: options ?? {} });
		return settled() as unknown as Animation;
	};
});
afterEach(() => {
	host.remove();
	delete (Element.prototype as { animate?: unknown }).animate;
	vi.useRealTimers();
});

/** The dot nearest the ripple's origin, the one that must land first. */
function nearestDot(origin: keyof typeof ORIGINS) {
	const [ox, oy] = ORIGINS[origin];
	const distance = DOTS.map(([cx, cy]) => Math.hypot(cx - ox, cy - oy));
	return distance.indexOf(Math.min(...distance));
}
const landings = () => played.filter((entry) => entry.options.fill === 'backwards' && entry.options.iterations === undefined);
/** The dot a landing belongs to: the order its <g> was built in. */
const dotOf = (entry: Played) => [...host.querySelectorAll('svg > g')].indexOf(entry.target as Element);

describe('livingLogo', () => {
	it('ships Sheen and a ripple from the C’s centre', () => {
		expect(SHEEN).toBe(true);
		expect(ORIGIN).toBe('centre');
		expect(DOTS).toHaveLength(38);
	});

	it('builds the mark still, with no animation at all, under reduced motion', () => {
		const logo = createLivingLogo(host, { reducedMotion: true });
		expect(host.querySelectorAll('svg > g')).toHaveLength(38);
		expect(played).toHaveLength(0);
		expect(logo.state.still).toBe(true);
		logo.destroy();
	});

	it('lands the dot at the ripple’s centre first and alone, then spreads outward to the farthest', () => {
		const logo = createLivingLogo(host, { entry: 'play', delay: 0 });
		const lands = landings();
		expect(lands).toHaveLength(38);
		const first = nearestDot(ORIGIN);
		const delays = lands.map((entry) => ({ dot: dotOf(entry), delay: Number(entry.options.delay) }));
		expect(delays.find((entry) => entry.dot === first)!.delay).toBe(0);
		// Every other dot waits until the first has well begun, so the ripple leaves a dot that is already in.
		const others = delays.filter((entry) => entry.dot !== first).map((entry) => entry.delay);
		expect(Math.min(...others)).toBeGreaterThanOrEqual(MOTION.originLead * MOTION.spreadMs);
		// The whole entry still takes spread + land, so the brand entrance keeps its length.
		expect(Math.max(...others)).toBeCloseTo(MOTION.spreadMs, 0);
		logo.destroy();
	});

	it('mirrors the order on exit: the centre dot leaves last, and the exit resolves', async () => {
		const logo = createLivingLogo(host, { entry: 'none' });
		played.length = 0;
		// Entry 'none' plays no landing, so the exit alone starts the dots moving.
		await logo.exit();
		const outs = played.filter((entry) => entry.options.fill === 'both');
		expect(outs).toHaveLength(38);
		const first = nearestDot(ORIGIN);
		const delays = outs.map((entry) => ({ dot: dotOf(entry), delay: Number(entry.options.delay) }));
		const last = delays.reduce((a, b) => (b.delay > a.delay ? b : a));
		expect(last.dot).toBe(first);
		expect(delays.find((entry) => entry.dot !== first && entry.delay === 0)).toBeTruthy();
		logo.destroy();
	});

	it('keeps pulsing after a hidden window shows again, even with no entry to play (the sign-in mark)', () => {
		vi.useFakeTimers();
		let hidden = true;
		Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
		try {
			const logo = createLivingLogo(host, { entry: 'none' });
			vi.advanceTimersByTime(60_000);
			const before = played.length;
			hidden = false;
			document.dispatchEvent(new Event('visibilitychange'));
			vi.advanceTimersByTime(60_000);
			expect(played.length).toBeGreaterThan(before);
			logo.destroy();
		} finally {
			delete (document as { hidden?: unknown }).hidden;
		}
	});

	it('removes itself on destroy', () => {
		const logo = createLivingLogo(host, { entry: 'none' });
		expect(host.querySelector('svg')).toBeTruthy();
		logo.destroy();
		expect(host.querySelector('svg')).toBeNull();
	});
});

describe('the launch shell page’s copy of the mark', () => {
	it('is the app’s module, regenerated (run node desktop/scripts/gen-shell-logo.mjs after changing livingLogo.js)', () => {
		const root = resolve(__dirname, '../../..');
		expect(() => execFileSync('node', [resolve(root, 'desktop/scripts/gen-shell-logo.mjs'), '--check'], { stdio: 'pipe' })).not.toThrow();
	});
});
