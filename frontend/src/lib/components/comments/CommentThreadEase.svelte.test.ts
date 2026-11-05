import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread, { type CommentThreadData } from './CommentThread.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); vi.restoreAllMocks(); });

/** Mounts a card whose data the test changes afterwards, and records every node a transition animates. */
function card(overrides: Partial<CommentThreadData>) {
	const comment = $state<CommentThreadData>({ id: 'c1', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [], ...overrides });
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(CommentThread, { target: host, props: { comment, kept: true, onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn() } });
	flushSync();
	const eased: Element[] = [];
	vi.spyOn(Element.prototype, 'animate').mockImplementation(function (this: Element) {
		eased.push(this);
		return { cancel() {}, finish() {}, addEventListener() {}, removeEventListener() {}, currentTime: 0, playState: 'finished', finished: Promise.resolve() } as unknown as Animation;
	});
	return { comment, eased };
}

describe('CommentThread easing of the rows that change the card height', () => {
	it('eases the Agreed line in, with the same clip and fade the resolved note has', () => {
		const { comment, eased } = card({});
		comment.acked = true; flushSync();
		expect(eased).toContain(host.querySelector('.ct-agreed'));
	});

	it('eases Agree and Resolve out when the thread is resolved, and back in on re-open', () => {
		const { comment, eased } = card({});
		// An unsent draft keeps the card in its card form when it is resolved (an empty one switches to the resolved summary).
		const field = host.querySelector('textarea')!;
		field.value = 'half a reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		const [agree, resolve] = Array.from(host.querySelectorAll('.ct-act'));
		comment.resolved = true; flushSync();
		expect(eased).toContain(agree);
		expect(eased).toContain(resolve);
		eased.length = 0;
		comment.resolved = false; flushSync();
		expect(eased).toContain(host.querySelector('.ct-act.ack'));
		expect(eased).toContain(host.querySelector('.ct-act.resolve'));
	});
});
