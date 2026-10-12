import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { capsuleClearance, trackCapsuleClearance } from './capsuleClearance';

let frames: Array<() => void> = [];
let resized: () => void = () => {};

beforeEach(() => {
	frames = [];
	vi.stubGlobal('requestAnimationFrame', (callback: () => void) => frames.push(callback));
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { resized = callback; }
		observe() {}
		unobserve() {}
		disconnect() {}
	});
});
afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren(); });

/** A viewer whose bottom edge is at `bottom` px, holding a capsule `height` px tall at the phone's floating position. */
function viewer(bottom: number, height = 60) {
	const host = document.createElement('div');
	host.getBoundingClientRect = () => ({ top: 0, bottom } as DOMRect);
	const capsule = document.createElement('div');
	capsule.className = 'rb-capsule';
	Object.assign(capsule.style, { position: 'fixed', bottom: '12px', left: '12px' });
	Object.defineProperty(capsule, 'offsetHeight', { configurable: true, get: () => height });
	host.append(capsule);
	document.body.append(host);
	return { host, capsule };
}
const flush = () => { while (frames.length) frames.shift()!(); };

describe('capsuleClearance', () => {
	it('is how far the floating capsule reaches into the viewer, plus its side inset as the gap above it', () => {
		// The capsule's top is at 852 - 12 - 60 = 780, so it covers the viewer's last 72 px; 12 more keep the ring clear.
		expect(capsuleClearance(viewer(852).host, 852)).toBe(84);
	});

	it('is zero while the capsule sits in its own row, is tucked away, is absent, or ends above the viewer', () => {
		const inRow = viewer(852);
		inRow.capsule.style.position = 'static';
		expect(capsuleClearance(inRow.host, 852)).toBe(0);
		const tucked = viewer(852);
		tucked.capsule.classList.add('suppressed');
		expect(capsuleClearance(tucked.host, 852)).toBe(0);
		const empty = document.createElement('div');
		expect(capsuleClearance(empty, 852)).toBe(0);
		expect(capsuleClearance(viewer(600).host, 852)).toBe(0);
	});
});

describe('trackCapsuleClearance', () => {
	it('sets the clearance at once, then follows the capsule as it tucks away and grows, a frame at a time', async () => {
		vi.stubGlobal('innerHeight', 852);
		const { host, capsule } = viewer(852);
		const tracker = trackCapsuleClearance(host);
		expect(host.style.getPropertyValue('--capsule-clearance')).toBe('84px');

		capsule.classList.add('suppressed');
		await Promise.resolve();
		expect(frames).toHaveLength(1);
		flush();
		expect(host.style.getPropertyValue('--capsule-clearance')).toBe('0px');

		capsule.classList.remove('suppressed');
		Object.defineProperty(capsule, 'offsetHeight', { configurable: true, get: () => 120 });
		resized();
		await Promise.resolve();
		expect(frames).toHaveLength(1);
		flush();
		expect(host.style.getPropertyValue('--capsule-clearance')).toBe('144px');

		tracker.destroy();
		window.dispatchEvent(new Event('resize'));
		expect(frames).toHaveLength(0);
	});
});
