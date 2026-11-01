import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { ANCHOR_MOVED } from './Popover.svelte';
import Fixture from './PopoverStickyFixture.svelte';

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
  // The person's own scroll moves the text: the card follows it out, focused or not.
  fixture.setRect(new DOMRect(400, 3000, 50, 20)); window.dispatchEvent(new Event('scroll')); flushSync();
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
