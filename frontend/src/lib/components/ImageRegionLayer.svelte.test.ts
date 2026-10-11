import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ImageRegionLayer from './ImageRegionLayer.svelte';
import type { DocComment } from '$lib/stores/docComments';
import type { ImageRegion } from '$lib/imageRegions';

// A moved pin fades in at its new place and out of its old one, and settles or
// reverts in place, so nothing pops (motion.md).
const OLD: ImageRegion = { x: 0.1, y: 0.1, w: 0.2, h: 0.2 };
const NEW: ImageRegion = { x: 0.6, y: 0.5, w: 0.2, h: 0.2 };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let animate: MockInstance<Element['animate']>;

function comment(region: ImageRegion, id = 'a', regionNumber = 1): DocComment {
	return { id, anchor: '', author: 'you', authorKind: 'user', text: id, at: 'now', resolved: false, replies: [], region, regionNumber };
}
function render(comments = [comment(OLD)]) {
	const props = $state({
		comments, moving: null as { id: string; region: ImageRegion | null } | null, selectedId: 'a' as string | null, scale: 1,
		colorOf: () => '#e5484d', draftColor: '#3e63dd', onPick: vi.fn(), onHover: vi.fn(), onDraw: vi.fn(), onDismiss: vi.fn(),
	});
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(ImageRegionLayer, { target: host, props });
	flushSync();
	return props;
}
/** The live marks and tags; one leaving stays in the DOM, inert, while it fades. */
const all = (selector: string) => [...host.querySelectorAll<HTMLElement>(selector)];
const live = (selector: string) => all(selector).find((element) => !element.inert) ?? null;
const leaving = (selector: string) => all(selector).find((element) => element.inert) ?? null;
/** The opacity keyframes the element was last faded with. */
function fadeOf(element: Element) {
	const call = animate.mock.calls.findLast(([frames], index) => animate.mock.contexts[index] === element && Array.isArray(frames) && frames.length > 0);
	return call ? { opacity: (call[0] as Keyframe[]).map((frame) => frame.opacity), ...(call[1] as KeyframeAnimationOptions) } : null;
}

let frame = new DOMRect(100, 50, 800, 500);
beforeEach(() => {
	frame = new DOMRect(100, 50, 800, 500);
	// A tag is where it is placed; everything else is the image's frame.
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return this.dataset.regionTag ? new DOMRect(frame.left + Number.parseFloat(this.style.left), frame.top + Number.parseFloat(this.style.top), 22, 22) : frame;
	});
	document.documentElement.style.setProperty('--dur-tight', '120ms');
	document.documentElement.style.setProperty('--ease-snap', 'cubic-bezier(0.2, 0, 0, 1)');
	animate = vi.spyOn(Element.prototype, 'animate');
});
afterEach(() => {
	if (component) unmount(component);
	component = null; host?.remove(); vi.restoreAllMocks();
	document.documentElement.style.removeProperty('--dur-tight');
	document.documentElement.style.removeProperty('--ease-snap');
});

describe('ImageRegionLayer moving a pin', () => {
	it('fades the pending pin in at its new place and keeps the old pin there, dashed, as the leaving mark', () => {
		const props = render();
		const box = live('[data-region-box="a"]')!, tag = live('[data-region-tag="1"]')!;
		props.moving = { id: 'a', region: NEW };
		flushSync();
		// The old pin's own box becomes the leaving mark: no new element appears where it was.
		expect(live('[data-region-from="a"]')).toBe(box);
		expect([box.style.left, box.classList.contains('leaving')]).toEqual(['10%', true]);
		const pending = live('[data-region-box="a"]')!;
		expect(pending).not.toBe(box);
		expect([pending.style.left, pending.classList.contains('pending')]).toEqual(['60%', true]);
		expect(fadeOf(pending)).toMatchObject({ opacity: [0, 1], duration: 120, easing: 'cubic-bezier(0.2, 0, 0, 1)' });
		// The tag fades out of the old place and in at the new one.
		const newTag = live('[data-region-tag="1"]')!;
		expect(newTag).not.toBe(tag);
		expect(newTag.classList.contains('pending')).toBe(true);
		expect(fadeOf(newTag)).toMatchObject({ opacity: [0, 1], duration: 120 });
		expect(leaving('[data-region-tag="1"]')).toBe(tag);
		expect(fadeOf(tag)).toMatchObject({ opacity: [1, 0], duration: 120 });
	});

	it('settles the pending pin in place once the move saves, and fades the old place out', () => {
		const props = render();
		props.moving = { id: 'a', region: NEW };
		flushSync();
		const pending = live('[data-region-box="a"]')!, pendingTag = live('[data-region-tag="1"]')!, old = live('[data-region-from="a"]')!;
		// The comment can take its new place before the move is marked done: the old place still shows where it was.
		props.comments = [comment({ ...NEW, x: 0.6000001 })];
		flushSync();
		expect(live('[data-region-from="a"]')).toBe(old);
		expect(old.style.left).toBe('10%');
		props.moving = null;
		flushSync();
		expect(live('[data-region-box="a"]')).toBe(pending);
		expect(pending.classList.contains('pending')).toBe(false);
		expect(live('[data-region-tag="1"]')).toBe(pendingTag);
		expect(pendingTag.classList.contains('pending')).toBe(false);
		expect(live('[data-region-from="a"]')).toBeNull();
		expect(leaving('[data-region-from="a"]')).toBe(old);
		expect(fadeOf(old)?.opacity.at(-1)).toBe(0);
	});

	it('gives the old pin back in place when the move fails, and fades the pending one out', () => {
		const props = render();
		const box = live('[data-region-box="a"]')!, tag = live('[data-region-tag="1"]')!;
		props.moving = { id: 'a', region: NEW };
		flushSync();
		const pending = live('[data-region-box="a"]')!, pendingTag = live('[data-region-tag="1"]')!;
		props.moving = null;
		flushSync();
		expect(live('[data-region-box="a"]')).toBe(box);
		expect([box.style.left, box.classList.contains('leaving')]).toEqual(['10%', false]);
		expect(live('[data-region-tag="1"]')).toBe(tag);
		expect(leaving('[data-region-box="a"]')).toBe(pending);
		expect(fadeOf(pending)?.opacity.at(-1)).toBe(0);
		expect(leaving('[data-region-tag="1"]')).toBe(pendingTag);
	});

	// Two boxes 16 px apart: the second's tag steps above the first's until the first moves away.
	const NEAR = [comment({ x: 0.4, y: 0.4, w: 0.1, h: 0.1 }), comment({ x: 0.42, y: 0.4, w: 0.1, h: 0.1 }, 'b', 2)];
	const glideOf = (element: Element) => animate.mock.calls.find(([frames], index) => animate.mock.contexts[index] === element && Array.isArray(frames) && 'translate' in (frames[0] ?? {}));

	it('glides a neighbour’s tag to the place a moved pin leaves free, rather than jumping', async () => {
		const props = render(NEAR);
		const neighbour = live('[data-region-tag="2"]')!;
		const before = neighbour.getBoundingClientRect();
		props.moving = { id: 'a', region: { x: 0.9, y: 0.9, w: 0, h: 0 } };
		flushSync();
		await Promise.resolve();
		const after = neighbour.getBoundingClientRect();
		expect([after.left, after.top]).not.toEqual([before.left, before.top]);
		const [frames, options] = glideOf(neighbour)!;
		expect(frames).toEqual([{ translate: `${before.left - after.left}px ${before.top - after.top}px` }, { translate: '0px 0px' }]);
		expect(options).toMatchObject({ duration: 120, easing: 'cubic-bezier(0.2, 0, 0, 1)' });
	});

	it('places tags at once when the image is zoomed, so they never trail it', async () => {
		const props = render(NEAR);
		const neighbour = live('[data-region-tag="2"]')!;
		const before = neighbour.style.left;
		frame = new DOMRect(100, 50, 1600, 1000);
		props.scale = 2;
		flushSync();
		await Promise.resolve();
		expect(neighbour.style.left).not.toBe(before);
		expect(glideOf(neighbour)).toBeUndefined();
	});
});
