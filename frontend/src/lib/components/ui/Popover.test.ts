import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import Popover from './Popover.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
function anchor(left = 8, top = 8): DOMRect { return new DOMRect(left, top, 10, 10); }
function render(options: { open?: boolean; anchor?: DOMRect; fill?: boolean } = {}) {
	host = document.createElement('div'); document.body.appendChild(host);
	const onClose = vi.fn(); const restoreFocus = vi.fn();
	const children = (() => {}) as any;
	component = mount(Popover, { target: host, props: { anchor: options.anchor ?? anchor(), open: options.open ?? true, fill: options.fill, onClose, restoreFocus, children } });
	flushSync(); return { onClose, restoreFocus };
}
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); });
describe('Popover', () => {
	it('gives its content a bounded flex column only when asked to fill', () => {
		render();
		expect(host.querySelector('.popover')!.classList.contains('fill')).toBe(false);
		unmount(component!); host.remove();
		render({ fill: true });
		expect(host.querySelector('.popover')!.classList.contains('fill')).toBe(true);
	});
	it('uses the subtle border token on the elevated surface', () => {
		const source = readFileSync(join(process.cwd(), 'src/lib/components/ui/Popover.svelte'), 'utf8');
		expect(source).toContain('border: 1px solid var(--border-subtle)');
	});

	it('clamps an anchored surface and closes on Escape', async () => {
		const { onClose, restoreFocus } = render({ anchor: anchor(window.innerWidth + 20, window.innerHeight + 20) });
		await Promise.resolve();
		expect(host.querySelector('.popover')).not.toBeNull();
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
		expect(onClose).toHaveBeenCalledOnce(); expect(restoreFocus).toHaveBeenCalledOnce();
	});
	it('clamps below the top edge when the anchor starts above the viewport', async () => {
		render({ anchor: anchor(8, -5) });
		await vi.waitFor(() => expect(host.querySelector<HTMLElement>('.popover')!.style.top).not.toBe('12px'));
		expect(host.querySelector<HTMLElement>('.popover')!.style.top).toBe('8px');
	});
	it('end-aligns a bottom surface that would overflow the right edge, and marks itself for the shell layer', async () => {
		const width = 200;
		const original = HTMLElement.prototype.getBoundingClientRect;
		HTMLElement.prototype.getBoundingClientRect = function () {
			return this.classList.contains('popover') ? new DOMRect(0, 0, width, 40) : original.call(this);
		};
		try {
			const right = window.innerWidth - 20;
			host = document.createElement('div'); document.body.appendChild(host);
			component = mount(Popover, { target: host, props: {
				anchor: new DOMRect(right - 10, 8, 10, 10), open: true, onClose: vi.fn(), placement: 'bottom', children: (() => {}) as any,
			} });
			flushSync();
			const surface = host.querySelector<HTMLElement>('.popover')!;
			expect(surface.hasAttribute('data-popover')).toBe(true);
			await vi.waitFor(() => expect(surface.style.left).toBe(`${right - width}px`));
		} finally {
			HTMLElement.prototype.getBoundingClientRect = original;
		}
	});
	it('closes on an outside pointerdown', () => {
		const { onClose } = render();
		document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		expect(onClose).toHaveBeenCalledOnce();
	});
	it('repositions on resize and closes when the anchor leaves the viewport', async () => {
		const { onClose } = render({ anchor: anchor(-50, -50) });
		await Promise.resolve();
		window.dispatchEvent(new Event('resize'));
		expect(onClose).toHaveBeenCalled();
	});
	describe('inside a canvas', () => {
		const original = HTMLElement.prototype.getBoundingClientRect;
		afterEach(() => { HTMLElement.prototype.getBoundingClientRect = original; });
		/** A canvas at `canvas` and a 380x170 surface; every other element measures as it would. */
		function measure(canvas: DOMRect) {
			HTMLElement.prototype.getBoundingClientRect = function () {
				if (this.classList.contains('popover')) return new DOMRect(0, 0, 380, 170);
				return this.hasAttribute('data-canvas') ? canvas : original.call(this);
			};
		}
		function mountIn(canvas: DOMRect, anchor: DOMRect | (() => DOMRect | null), onClose = vi.fn()) {
			measure(canvas);
			host = document.createElement('div'); document.body.appendChild(host);
			const bounds = document.createElement('div'); bounds.setAttribute('data-canvas', ''); host.appendChild(bounds);
			component = mount(Popover, { target: host, props: { anchor, bounds, open: true, onClose, children: (() => {}) as any } });
			flushSync();
			return host.querySelector<HTMLElement>('.popover')!;
		}
		it('opens beside the anchor and flips below it rather than leaving the canvas', async () => {
			const roomy = mountIn(new DOMRect(500, 0, 900, 700), new DOMRect(520, 100, 20, 20));
			await vi.waitFor(() => expect(roomy.style.left).toBe('548px'));
			expect(roomy.style.top).toBe('100px');
			unmount(component!); host.remove();
			// A canvas too narrow for the card on either side of the anchor: it goes under the anchor, clamped to the canvas.
			const narrow = mountIn(new DOMRect(500, 0, 420, 700), new DOMRect(700, 100, 20, 20));
			await vi.waitFor(() => expect(narrow.style.top).toBe('128px'));
			expect(narrow.style.left).toBe('532px');
		});
		it('sits below an anchor that spans the canvas, never over it', async () => {
			const surface = mountIn(new DOMRect(500, 0, 900, 700), new DOMRect(500, 300, 900, 50));
			await vi.waitFor(() => expect(surface.style.top).toBe('358px'));
			unmount(component!); host.remove();
			// Too low for a card under the anchor: above it.
			const low = mountIn(new DOMRect(500, 0, 900, 700), new DOMRect(500, 600, 900, 50));
			await vi.waitFor(() => expect(low.style.top).toBe('422px'));
		});
		it('uses empty canvas below an avoided image before covering another image tag', async () => {
			measure(new DOMRect(500, 0, 420, 700));
			host = document.createElement('div'); document.body.appendChild(host);
			const bounds = document.createElement('div'); bounds.setAttribute('data-canvas', ''); host.appendChild(bounds);
			component = mount(Popover, { target: host, props: {
				anchor: new DOMRect(700, 260, 20, 20), bounds, avoidRect: () => new DOMRect(560, 100, 300, 400),
				open: true, onClose: vi.fn(), children: (() => {}) as any,
			} });
			flushSync();
			const surface = host.querySelector<HTMLElement>('.popover')!;
			await vi.waitFor(() => expect(surface.style.top).toBe('508px'));
		});
		it('never flips onto the pane to the left of the canvas', async () => {
			const surface = mountIn(new DOMRect(500, 0, 520, 700), new DOMRect(900, 100, 20, 20));
			await vi.waitFor(() => expect(surface.style.left).not.toBe('12px'));
			expect(parseFloat(surface.style.left)).toBeGreaterThanOrEqual(508);
			expect(parseFloat(surface.style.left) + 380).toBeLessThanOrEqual(1012);
		});
		it('follows an anchor that moves when a scroller inside the canvas scrolls', async () => {
			let top = 400;
			const surface = mountIn(new DOMRect(500, 0, 900, 700), () => new DOMRect(520, top, 20, 20));
			await vi.waitFor(() => expect(surface.style.top).toBe('400px'));
			top = 150;
			const scroller = host.appendChild(document.createElement('div'));
			scroller.dispatchEvent(new Event('scroll'));
			await vi.waitFor(() => expect(surface.style.top).toBe('150px'));
		});
		it('hides a live anchor that is not drawn, without closing, and shows it again beside its text', async () => {
			let reading: DOMRect | null = new DOMRect(520, 100, 20, 20);
			const onClose = vi.fn();
			const surface = mountIn(new DOMRect(500, 0, 900, 700), () => reading, onClose);
			await vi.waitFor(() => expect(surface.style.top).toBe('100px'));
			reading = null;
			window.dispatchEvent(new Event('scroll'));
			await vi.waitFor(() => expect(surface.style.visibility).toBe('hidden'));
			expect(onClose).not.toHaveBeenCalled();
			reading = new DOMRect(520, 300, 20, 20);
			window.dispatchEvent(new Event('scroll'));
			await vi.waitFor(() => expect(surface.style.visibility).toBe(''));
			expect(surface.style.top).toBe('300px');
		});
		it('hides a live anchor scrolled out of the canvas instead of closing, while a fixed anchor still closes', async () => {
			let reading = new DOMRect(520, 100, 20, 20);
			const onClose = vi.fn();
			const surface = mountIn(new DOMRect(500, 0, 900, 700), () => reading, onClose);
			await vi.waitFor(() => expect(surface.style.top).toBe('100px'));
			reading = new DOMRect(520, 2000, 20, 20);
			window.dispatchEvent(new Event('scroll'));
			await vi.waitFor(() => expect(surface.style.visibility).toBe('hidden'));
			expect(onClose).not.toHaveBeenCalled();
			reading = new DOMRect(520, 220, 20, 20);
			window.dispatchEvent(new Event('scroll'));
			await vi.waitFor(() => expect(surface.style.visibility).toBe(''));
			expect(surface.style.top).toBe('220px');
			unmount(component!); host.remove();
			const fixed = vi.fn();
			mountIn(new DOMRect(500, 0, 900, 700), new DOMRect(520, 2000, 20, 20), fixed);
			await vi.waitFor(() => expect(fixed).toHaveBeenCalled());
		});
	});

	it('places the surface at once when its anchor is replaced, without a scroll or an edit', async () => {
		const { fromStore, writable } = await import('svelte/store');
		const current = writable<DOMRect | (() => DOMRect | null)>(() => new DOMRect(520, 100, 20, 20));
		const live = fromStore(current);
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(Popover, { target: host, props: { get anchor() { return live.current; }, open: true, onClose: vi.fn(), children: (() => {}) as any } });
		flushSync();
		const surface = () => host.querySelector<HTMLElement>('.popover')!;
		await vi.waitFor(() => expect(surface().style.top).toBe('100px'));
		current.set(new DOMRect(520, 300, 20, 20)); flushSync();
		await vi.waitFor(() => expect(surface().style.top).toBe('300px'));
	});
});
