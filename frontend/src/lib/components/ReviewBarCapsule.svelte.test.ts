import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ReviewBar from './ReviewBar.svelte';
import type { ReviewBarRead } from '$lib/api/reviewBar';

// The room the review bar reports for its floating phone capsule (`capsuleClearance`), which the image viewer
// leaves below its canvas so the capsule never covers the canvas's edge or its focus ring.

const api = vi.hoisted(() => ({ fetchReviewBar: vi.fn(), answerReview: vi.fn() }));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));

const READ: ReviewBarRead = {
	ask_raised: true, hold_ids: ['h1'], ask_reason: 'Is this screen ready?', ask_raised_at: '2026-10-10T10:00:00',
	asker: { id: 'a1', name: 'Docs agent' }, other_askers: [], other_asks: [], other_request_count: 0,
	round_feedback_count: 0, round_feedback_counts: { comments: 0, suggestions: 0, edits: 0 },
};

let container: HTMLElement;
let component: Record<string, unknown> | null = null;
let resized: () => void = () => {};
let observed: Element[] = [];
let clearance = $state(-1);
let suppressed = $state(false);

beforeEach(() => {
	clearance = -1;
	suppressed = false;
	observed = [];
	api.fetchReviewBar.mockReset().mockResolvedValue(READ);
	vi.stubGlobal('innerHeight', 852);
	vi.stubGlobal('ResizeObserver', class {
		constructor(callback: () => void) { resized = callback; }
		observe(element: Element) { observed.push(element); }
		disconnect() { observed = []; }
	});
	container = document.createElement('div');
	document.body.append(container);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	container.remove();
	vi.unstubAllGlobals();
});

/** The bar inside a viewer whose bottom edge is at `bottom` px, its capsule `height` px tall at the phone's floating position. */
async function render(bottom = 852, height = 60) {
	component = mount(ReviewBar, {
		target: container,
		props: {
			artifactId: 'img-1',
			get suppressed() { return suppressed; },
			get capsuleClearance() { return clearance; },
			set capsuleClearance(value: number) { clearance = value; },
		},
	});
	await vi.waitFor(() => expect(container.querySelector('.rb-capsule')).toBeTruthy());
	flushSync();
	const slot = container.querySelector<HTMLElement>('.rb-slot')!;
	const viewer = { getBoundingClientRect: () => ({ top: 0, bottom } as DOMRect) };
	Object.defineProperty(slot, 'offsetParent', { configurable: true, get: () => viewer });
	const capsule = container.querySelector<HTMLElement>('.rb-capsule')!;
	Object.defineProperty(capsule, 'offsetHeight', { configurable: true, get: () => height });
	return capsule;
}
/** Puts the capsule where the phone stylesheet floats it, and lets the bar measure. */
function float(capsule: HTMLElement) {
	Object.assign(capsule.style, { position: 'fixed', bottom: '12px', left: '12px' });
	window.dispatchEvent(new Event('resize'));
	flushSync();
}

describe('ReviewBar capsule clearance', () => {
	it('is how far the floating capsule reaches into its container, plus its side inset as the gap above it', async () => {
		const capsule = await render();
		// In the bar's own row (no phone stylesheet in the test), it covers nothing.
		expect(clearance).toBe(0);
		expect(observed).toContain(capsule);
		float(capsule);
		// The capsule's top is at 852 - 12 - 60 = 780, so it covers the viewer's last 72 px; 12 more keep the ring clear.
		expect(clearance).toBe(84);
	});

	it('is zero while the capsule is tucked away, and comes back when it returns', async () => {
		const capsule = await render();
		float(capsule);
		suppressed = true;
		flushSync();
		expect(clearance).toBe(0);
		suppressed = false;
		flushSync();
		expect(clearance).toBe(84);
	});

	it('is zero when the capsule ends above the container', async () => {
		float(await render(600));
		expect(clearance).toBe(0);
	});

	it('follows the capsule as it grows, from its own size', async () => {
		const capsule = await render();
		float(capsule);
		Object.defineProperty(capsule, 'offsetHeight', { configurable: true, get: () => 120 });
		resized();
		flushSync();
		expect(clearance).toBe(144);
	});

	it('stops measuring once the bar is gone', async () => {
		float(await render());
		unmount(component!);
		component = null;
		expect(observed).toHaveLength(0);
		clearance = -1;
		window.dispatchEvent(new Event('resize'));
		expect(clearance).toBe(-1);
	});
});
