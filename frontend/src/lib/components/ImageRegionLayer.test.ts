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
		// Only the picked comment's box is drawn.
		expect(host.querySelectorAll('[data-region-box]')).toHaveLength(1);
		expect(tags[1].getAttribute('aria-label')).toBe('Comment 2 by you, box 20% wide, 20% tall at 40% across, 30% down');
	});

	it("names an agent on its tag and marks a pin on a changed image", () => {
		render({ comments: [comment('a', 3, { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, { author: 'Design QA', authorKind: 'agent', regionStale: true })] });
		const tag = host.querySelector<HTMLElement>('[data-region-tag]')!;
		expect(tag.textContent).toBe('3Design QA');
		expect(tag.classList.contains('stale')).toBe(true);
		expect(tag.getAttribute('aria-label')).toContain('image changed since this comment');
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
		// Two points close together: tag 1 sits up-right of (0.5, 0.5), tag 2 up-right of (0.52, 0.5).
		const { onPick } = render({ comments: [comment('a', 1, { x: 0.5, y: 0.5, w: 0, h: 0 }), comment('b', 2, { x: 0.52, y: 0.5, w: 0, h: 0 })] });
		const [first, second] = host.querySelectorAll<HTMLElement>('[data-region-tag]');
		// On screen tag 2's centre is at 100 + 416 + 15.4 = 531.4; a press there on tag 1's element goes to tag 2.
		first.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1, clientX: 531, clientY: 285 }));
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
