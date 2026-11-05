import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { writable } from 'svelte/store';

vi.mock('$lib/stores/viewport', async (importOriginal) => ({ ...await importOriginal<object>(), prefersReducedMotion: writable(true) }));
import CommentThread from './CommentThread.svelte';

let host: HTMLElement;
let component: Record<string, unknown>;
afterEach(() => { if (component) unmount(component); host?.remove(); vi.restoreAllMocks(); document.body.innerHTML = ''; });

describe('Agree reduced motion', () => {
	it('crossfades the card through the shared layout crossfade when the agreed line changes', async () => {
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 300, height: 80 } as DOMRect);
		const faded: Element[] = [];
		const animate = Element.prototype.animate;
		vi.spyOn(Element.prototype, 'animate').mockImplementation(function (this: Element, ...args) { faded.push(this); return animate.apply(this, args); });
		const comment = $state({ id: 'c', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, acked: false, replies: [] });
		host = document.createElement('div'); document.body.append(host);
		component = mount(CommentThread, { target: host, props: { comment } }); flushSync();
		comment.acked = true; flushSync(); await tick();
		const card = host.querySelector('.ct');
		const copy = Array.from(document.body.children).find((node) => node !== host && node.getAttribute('aria-hidden') === 'true');
		expect(copy?.classList.contains('ct')).toBe(true);
		expect(faded).toEqual(expect.arrayContaining([copy, card]));
		// The line itself cuts: the card's crossfade carries it.
		expect(faded).not.toContain(host.querySelector('.ct-agreed'));
	});
});
