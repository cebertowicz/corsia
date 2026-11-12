import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import Popover, { ANCHOR_MOVED } from './Popover.svelte';
import Fixture from '$lib/__tests__/fixtures/PopoverStickyFixture.svelte';

it('keeps its opening side while it grows, then chooses again for a new anchor or open', async () => {
	let height = 100;
	const host = document.createElement('div'); document.body.appendChild(host);
	const measure = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		return this.classList.contains('popover') ? new DOMRect(0, 0, 200, height) : new DOMRect();
	});
	const width = vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
	const tall = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(() => height);
	const fixture = mount(Fixture, { target: host }); flushSync();
	try {
		const surface = () => document.body.querySelector<HTMLElement>('.popover')!;
		await vi.waitFor(() => expect(surface().style.top).toBe('428px'));
		expect(surface().style.maxHeight).toContain(`${window.innerHeight - 428 - 8}px`);
		height = 350; window.dispatchEvent(new Event(ANCHOR_MOVED)); flushSync();
		expect(surface().style.top).toBe(`${window.innerHeight - height - 8}px`);
		flushSync(() => fixture.setKey('second'));
		await vi.waitFor(() => expect(surface().style.top).toBe('42px'));
		height = 100; window.dispatchEvent(new Event(ANCHOR_MOVED)); flushSync();
		expect(surface().style.top).toBe('292px');
		flushSync(() => fixture.setOpen(false));
		flushSync(() => fixture.setOpen(true));
		await vi.waitFor(() => expect(surface().style.top).toBe('428px'));
	} finally { await unmount(fixture); host.remove(); measure.mockRestore(); width.mockRestore(); tall.mockRestore(); }
});

it('ignores a scroll that left its anchor in place, and answers one that moved it', async () => {
	let height = 100;
	const host = document.createElement('div'); document.body.appendChild(host);
	const elsewhere = document.createElement('div'); document.body.appendChild(elsewhere);
	const width = vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
	const tall = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(() => height);
	const fixture = mount(Fixture, { target: host }); flushSync();
	try {
		const surface = () => document.body.querySelector<HTMLElement>('.popover')!;
		const field = () => surface().querySelector('textarea')!;
		await vi.waitFor(() => expect(document.activeElement).toBe(field()));
		// The person is typing and an edit pushed the text out of the area: the card stays, clamped.
		fixture.setRect(new DOMRect(400, 2000, 50, 20)); window.dispatchEvent(new Event(ANCHOR_MOVED)); flushSync();
		expect(surface().getAttribute('aria-hidden')).toBeNull();
		for (const scroller of [field(), surface(), elsewhere]) {
			scroller.dispatchEvent(new Event('scroll')); flushSync();
			expect(surface().getAttribute('aria-hidden')).toBeNull();
			expect(document.activeElement).toBe(field());
		}
		// A scroll the document caused, with no wheel or touch behind it, leaves the card they are typing in where it is.
		fixture.setRect(new DOMRect(400, 3000, 50, 20)); window.dispatchEvent(new Event('scroll')); flushSync();
		expect(surface().getAttribute('aria-hidden')).toBeNull();
		expect(document.activeElement).toBe(field());
		// The person's own scroll moves the text: the card follows it out, focused or not.
		fixture.setRect(new DOMRect(400, 3500, 50, 20)); window.dispatchEvent(new Event('wheel')); window.dispatchEvent(new Event('scroll')); flushSync();
		expect(surface().getAttribute('aria-hidden')).toBe('true');
		fixture.setRect(new DOMRect(400, 400, 50, 20)); window.dispatchEvent(new Event('scroll')); flushSync();
		expect(surface().getAttribute('aria-hidden')).toBeNull();
		expect(surface().style.top).toBe('428px');
		// Grown past the area it clamps over the anchor, until a scroll moves the anchor and the side is chosen again.
		height = 350; window.dispatchEvent(new Event(ANCHOR_MOVED)); flushSync();
		expect(surface().style.top).toBe(`${window.innerHeight - height - 8}px`);
		elsewhere.dispatchEvent(new Event('scroll')); flushSync();
		expect(surface().style.top).toBe(`${window.innerHeight - height - 8}px`);
		fixture.setRect(new DOMRect(400, 410, 50, 20)); window.dispatchEvent(new Event('scroll')); flushSync();
		expect(surface().style.top).toBe('52px');
	} finally { await unmount(fixture); host.remove(); elsewhere.remove(); width.mockRestore(); tall.mockRestore(); }
});

it('keeps the side a click-opened card chose while it grows, and does not flip above the line', async () => {
	let height = 100;
	const host = document.createElement('div'); document.body.appendChild(host);
	const width = vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
	const tall = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(() => height);
	const line = new DOMRect(200, 100, 0, 20);
	const mountAt = () => mount(Popover, { target: host, props: { anchor: line, open: true, onClose: vi.fn(), placement: 'pointer', children: (() => {}) as any } });
	let card = mountAt(); flushSync();
	try {
		const surface = () => document.body.querySelector<HTMLElement>('.popover')!;
		await vi.waitFor(() => expect(surface().style.top).toBe('124px'));
		// Too tall to fit under the line: it stays under it, clamped into the screen, rather than jumping above the line.
		height = window.innerHeight - 20; window.dispatchEvent(new Event(ANCHOR_MOVED)); flushSync();
		expect(surface().style.top).toBe(`${window.innerHeight - height - 8}px`);
		expect(surface().style.left).toBe('212px');
	} finally { await unmount(card); host.remove(); width.mockRestore(); tall.mockRestore(); }
});

it('clamps into the screen when its own content grows, with no other signal', async () => {
	let height = 100;
	const grown: (() => void)[] = [];
	const observer = vi.spyOn(globalThis, 'ResizeObserver').mockImplementation(function (callback: ResizeObserverCallback) {
		grown.push(() => callback([], {} as ResizeObserver));
		return { observe() {}, unobserve() {}, disconnect() {} } as unknown as ResizeObserver;
	} as unknown as typeof ResizeObserver);
	const host = document.createElement('div'); document.body.appendChild(host);
	const width = vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
	const tall = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(() => height);
	const card = mount(Popover, { target: host, props: { anchor: new DOMRect(200, 100, 0, 20), open: true, onClose: vi.fn(), placement: 'pointer', children: (() => {}) as any } });
	flushSync();
	try {
		const surface = () => document.body.querySelector<HTMLElement>('.popover')!;
		await vi.waitFor(() => expect(surface().style.top).toBe('124px'));
		// A reply typed into the card makes it taller: its bottom stays on screen, so Send does.
		height = window.innerHeight - 20; grown.forEach((fire) => fire()); flushSync();
		expect(surface().style.top).toBe(`${window.innerHeight - height - 8}px`);
	} finally { await unmount(card); host.remove(); width.mockRestore(); tall.mockRestore(); observer.mockRestore(); }
});

it('repositions and limits the card when the visual viewport changes', async () => {
 const viewport = Object.assign(new EventTarget(), { offsetTop: 0, height: 700 });
 const original = Object.getOwnPropertyDescriptor(window, 'visualViewport');
 Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
 const host = document.createElement('div'); document.body.appendChild(host);
 const width = vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
 const tall = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(100);
 const fixture = mount(Fixture, { target: host }); flushSync();
 try {
  const surface = () => document.body.querySelector<HTMLElement>('.popover')!;
  await vi.waitFor(() => expect(surface().style.top).toBe('428px'));
  expect(surface().style.maxHeight).toContain('264px');
  viewport.height = 400; viewport.offsetTop = 100;
  viewport.dispatchEvent(new Event('scroll')); flushSync();
  expect(surface().style.top).toBe('392px');
  expect(surface().style.maxHeight).toContain('100px');
 } finally {
  await unmount(fixture); host.remove(); width.mockRestore(); tall.mockRestore();
  if (original) Object.defineProperty(window, 'visualViewport', original);
  else Reflect.deleteProperty(window, 'visualViewport');
 }
});
