import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ImageRegionLayer from './ImageRegionLayer.svelte';
import type { DocComment } from '$lib/stores/docComments';

const FRAME = new DOMRect(100, 50, 800, 500);
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function comment(id: string, number: number, region: DocComment['region'], extra: Partial<DocComment> = {}): DocComment {
	return { id, anchor: '', author: 'you', authorKind: 'user', text: id, at: 'now', resolved: false, replies: [], region, regionNumber: number, ...extra };
}
function render(props: Record<string, unknown> = {}) {
	host = document.createElement('div'); document.body.appendChild(host);
	const handlers = { onPick: vi.fn(), onHover: vi.fn(), onDraw: vi.fn(), onDismiss: vi.fn() };
	component = mount(ImageRegionLayer, { target: host, props: { comments: [], colorOf: () => '#e5484d', draftColor: '#3e63dd', ...handlers, ...props } });
	flushSync();
	return { ...handlers, layer: host.querySelector<HTMLElement>('[data-image-regions]')! };
}
function pointer(type: string, x: number, y: number, extra: PointerEventInit = {}) {
	return new PointerEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, pointerId: 1, pointerType: 'mouse', ...extra });
}

beforeEach(() => {
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(FRAME);
	// jsdom has no PointerEvent; a MouseEvent carrying the pointer fields stands in.
	if (typeof PointerEvent === 'undefined') {
		(globalThis as unknown as { PointerEvent: unknown }).PointerEvent = class extends MouseEvent {
			pointerId: number; pointerType: string;
			constructor(type: string, init: PointerEventInit = {}) { super(type, init); this.pointerId = init.pointerId ?? 0; this.pointerType = init.pointerType ?? 'mouse'; }
		};
	}
});
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); vi.restoreAllMocks(); });

describe('ImageRegionLayer', () => {
	it('numbers tags in order, faint until picked, outside the box they point at', () => {
		render({ comments: [comment('b', 2, { x: 0.4, y: 0.3, w: 0.2, h: 0.2 }), comment('a', 1, { x: 0.5, y: 0.5, w: 0, h: 0 })], selectedId: 'b' });
		const tags = [...host.querySelectorAll<HTMLElement>('[data-region-tag]')];
		expect(tags.map((tag) => tag.dataset.regionTag)).toEqual(['1', '2']);
		expect(tags[0].classList.contains('faint')).toBe(true);
		expect(tags[1].classList.contains('faint')).toBe(false);
		// Left of the box's top-left corner, matching the server's marked-up copy.
		expect(tags[1].style.left).toBe('304px');
		expect(tags[1].style.top).toBe('163px');
		// Only the picked comment's box shows.
		expect(host.querySelectorAll('[data-region-box].shown')).toHaveLength(1);
		expect(tags[1].getAttribute('aria-label')).toBe('Comment 2 by you, box 20% wide, 20% tall at 40% across, 30% down');
	});

	it('keeps every box mounted so it fades in and out on hover instead of popping', async () => {
		render({ comments: [comment('a', 1, { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }), comment('b', 2, { x: 0.5, y: 0.5, w: 0.2, h: 0.2 })] });
		const boxes = () => [...host.querySelectorAll<HTMLElement>('[data-region-box]')];
		expect(boxes().map((box) => box.dataset.regionBox)).toEqual(['a', 'b']);
		expect(boxes().filter((box) => box.classList.contains('shown'))).toHaveLength(0);
		host.querySelector<HTMLElement>('[data-region-tag="2"]')!.dispatchEvent(new FocusEvent('focus'));
		flushSync();
		expect(boxes().filter((box) => box.classList.contains('shown')).map((box) => box.dataset.regionBox)).toEqual(['b']);
	});

	it('hides an agent name at rest when it covers another comment’s point', () => {
		vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
			if (this.classList.contains('name')) return new DOMRect(155, 110, 50, 18);
			if (this.getAttribute('data-region-dot') === 'b') return new DOMRect(180, 113, 14, 14);
			if (this.getAttribute('data-region-tag') === '1') return new DOMRect(110, 110, 22, 22);
			if (this.getAttribute('data-region-tag') === '2') return new DOMRect(500, 400, 22, 22);
			return FRAME;
		});
		render({ comments: [
			comment('a', 1, { x: 0.1, y: 0.1, w: 0.1, h: 0.1 }, { author: 'Reviewer', authorKind: 'agent' }),
			comment('b', 2, { x: 0.12, y: 0.13, w: 0, h: 0 }),
		] });
		flushSync();
		expect(host.querySelector('[data-region-tag="1"] .name')?.classList.contains('hidden')).toBe(true);
	});

	it('shows a pin being moved at its new place, pending, and its old place as leaving', () => {
		render({
			comments: [comment('a', 1, { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, { regionStale: true })],
			moving: { id: 'a', region: { x: 0.6, y: 0.5, w: 0.2, h: 0.2 } },
		});
		const tag = host.querySelector<HTMLElement>('[data-region-tag="1"]')!;
		expect(tag.classList.contains('pending')).toBe(true);
		expect(Number.parseFloat(tag.style.left)).toBeGreaterThan(400);
		expect(tag.getAttribute('aria-label')).toContain('moving here');
		const next = host.querySelector<HTMLElement>('[data-region-box="a"]')!;
		expect([next.style.left, next.classList.contains('pending'), next.classList.contains('shown')]).toEqual(['60%', true, true]);
		const old = host.querySelector<HTMLElement>('[data-region-from="a"]')!;
		expect([old.style.left, old.classList.contains('leaving')]).toEqual(['10%', true]);
	});

	it('shows a moved point pending at its new spot rather than as a bare dot', () => {
		render({
			comments: [comment('a', 1, { x: 0.1, y: 0.1, w: 0, h: 0 })],
			moving: { id: 'a', region: { x: 0.7, y: 0.6, w: 0, h: 0 } },
		});
		const next = host.querySelector<HTMLElement>('[data-region-dot="a"]')!;
		expect([next.style.left, next.classList.contains('pending')]).toEqual(['70%', true]);
		expect(host.querySelector<HTMLElement>('[data-region-from="a"]')?.style.left).toBe('10%');
		expect(host.querySelector('.dot.draft')).toBeNull();
	});

	it("names an agent on its tag and marks a pin on a changed image", () => {
		render({ comments: [comment('a', 3, { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, { author: 'Design QA', authorKind: 'agent', regionStale: true })] });
		const tag = host.querySelector<HTMLElement>('[data-region-tag]')!;
		expect(tag.textContent).toBe('3Design QA');
		expect(tag.classList.contains('stale')).toBe(true);
		expect(tag.getAttribute('aria-label')).toContain('image changed since this comment');
	});

	it('hides an agent name at rest only when it overlaps another tag, then reveals it on focus', () => {
		vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (this: HTMLElement) {
			if (this.classList.contains('name')) {
				return this.closest('[data-comment-id]')?.getAttribute('data-comment-id') === 'a'
					? new DOMRect(155, 110, 50, 18) : new DOMRect(210, 110, 50, 18);
			}
			if (this.getAttribute('data-region-tag') === '1') return new DOMRect(110, 110, 22, 22);
			if (this.getAttribute('data-region-tag') === '2') return new DOMRect(160, 110, 22, 22);
			// Each point's dot is 14 px at its fraction of the frame, clear of both names.
			if (this.getAttribute('data-region-dot') === 'a') return new DOMRect(173, 93, 14, 14);
			if (this.getAttribute('data-region-dot') === 'b') return new DOMRect(253, 93, 14, 14);
			return FRAME;
		});
		render({ comments: [
			comment('a', 1, { x: 0.1, y: 0.1, w: 0, h: 0 }, { author: 'Reviewer one', authorKind: 'agent' }),
			comment('b', 2, { x: 0.2, y: 0.1, w: 0, h: 0 }, { author: 'Reviewer two', authorKind: 'agent' }),
		] });
		flushSync();
		const [first, second] = host.querySelectorAll<HTMLElement>('[data-region-tag]');
		expect(first.querySelector('.name')?.classList.contains('hidden')).toBe(true);
		expect(second.querySelector('.name')?.classList.contains('hidden')).toBe(false);
		first.dispatchEvent(new FocusEvent('focus'));
		flushSync();
		expect(first.querySelector('.name')?.classList.contains('hidden')).toBe(false);
	});

	it('hides a resolved pin unless it is the one picked', () => {
		const resolved = comment('r', 1, { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, { resolved: true });
		render({ comments: [resolved] });
		expect(host.querySelector('[data-region-tag]')).toBeNull();
		unmount(component!); host.remove();
		render({ comments: [resolved], selectedId: 'r' });
		expect(host.querySelector('[data-region-tag]')).not.toBeNull();
	});

	it('draws a box from a mouse drag, in fractions of the image, and keeps the viewer from panning', () => {
		const { layer, onDraw } = render();
		// Svelte delegates the viewer's handler to the app root, so a press that reaches the document has reached the viewer.
		const viewer = vi.fn();
		document.addEventListener('pointerdown', viewer);
		layer.dispatchEvent(pointer('pointerdown', 180, 150));
		layer.dispatchEvent(pointer('pointermove', 400, 300));
		flushSync();
		expect(host.querySelector('[data-region-draft]')).not.toBeNull();
		layer.dispatchEvent(pointer('pointerup', 500, 350));
		document.removeEventListener('pointerdown', viewer);
		expect(viewer).not.toHaveBeenCalled();
		expect(onDraw).toHaveBeenCalledOnce();
		expect(onDraw.mock.calls[0][0]).toEqual({ x: 0.1, y: 0.2, w: 0.4, h: 0.4 });
		expect(onDraw.mock.calls[0][1]()).toMatchObject({ left: 180, top: 150, width: 320, height: 200 });
	});

	it('places a point from a click, and a click with a card open only closes the card', () => {
		const { layer, onDraw, onDismiss } = render();
		layer.dispatchEvent(pointer('pointerdown', 500, 300));
		layer.dispatchEvent(pointer('pointerup', 502, 301));
		expect(onDraw.mock.calls[0][0]).toEqual({ x: 0.5, y: 0.5, w: 0, h: 0 });
		unmount(component!); host.remove();
		const open = render({ cardOpen: true });
		open.layer.dispatchEvent(pointer('pointerdown', 500, 300));
		open.layer.dispatchEvent(pointer('pointerup', 500, 300));
		expect(open.onDismiss).toHaveBeenCalledOnce();
		expect(open.onDraw).not.toHaveBeenCalled();
		expect(onDismiss).not.toHaveBeenCalled();
	});

	it('lets one finger pan outside box mode and draw in it', () => {
		const touch = { pointerType: 'touch' };
		const { layer, onDraw } = render();
		layer.dispatchEvent(pointer('pointerdown', 200, 100, touch));
		layer.dispatchEvent(pointer('pointerup', 400, 300, touch));
		expect(onDraw).not.toHaveBeenCalled();
		unmount(component!); host.remove();
		const boxed = render({ boxMode: true });
		boxed.layer.dispatchEvent(pointer('pointerdown', 200, 100, touch));
		boxed.layer.dispatchEvent(pointer('pointerup', 400, 300, touch));
		expect(boxed.onDraw).toHaveBeenCalledOnce();
	});

	it('pans instead of drawing while Space is held and exposes the cursor state', () => {
		const { layer, onDraw } = render();
		expect(layer.classList.contains('drawing')).toBe(true);
		expect(layer.classList.contains('space-held')).toBe(false);
		window.dispatchEvent(new KeyboardEvent('keydown', { code: 'Space', key: ' ' }));
		flushSync();
		expect(layer.classList.contains('space-held')).toBe(true);
		layer.dispatchEvent(pointer('pointerdown', 200, 100));
		layer.dispatchEvent(pointer('pointerup', 400, 300));
		window.dispatchEvent(new KeyboardEvent('keyup', { code: 'Space', key: ' ' }));
		flushSync();
		expect(layer.classList.contains('space-held')).toBe(false);
		expect(onDraw).not.toHaveBeenCalled();
	});

	it('gives overlapping tag targets to the nearer centre and a key press to the focused tag', () => {
		const { onPick } = render({ comments: [comment('a', 1, { x: 0.5, y: 0.5, w: 0, h: 0 }), comment('b', 2, { x: 0.52, y: 0.5, w: 0, h: 0 })] });
		const [first, second] = host.querySelectorAll<HTMLElement>('[data-region-tag]');
		// The 44 px targets still overlap after collision-avoiding placement; use the rendered second centre.
		const secondCentre = { x: FRAME.left + Number.parseFloat(second.style.left), y: FRAME.top + Number.parseFloat(second.style.top) };
		first.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1, clientX: secondCentre.x, clientY: secondCentre.y }));
		expect(onPick.mock.calls[0][0]).toBe('b');
		second.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
		expect(onPick.mock.calls[1][0]).toBe('b');
		first.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 }));
		expect(onPick.mock.calls[2][0]).toBe('a');
	});

	it('comments on the whole image with Enter in box mode', () => {
		const { onDraw } = render({ boxMode: true });
		const surface = host.querySelector<HTMLElement>('[role="button"]')!;
		surface.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		expect(onDraw.mock.calls[0][0]).toEqual({ x: 0, y: 0, w: 1, h: 1 });
	});

	it('draws nothing when the reader may not comment', () => {
		const { layer, onDraw } = render({ readOnly: true, boxMode: true });
		layer.dispatchEvent(pointer('pointerdown', 200, 100));
		layer.dispatchEvent(pointer('pointerup', 400, 300));
		expect(onDraw).not.toHaveBeenCalled();
		expect(host.querySelector('[role="button"]')).toBeNull();
	});
});
