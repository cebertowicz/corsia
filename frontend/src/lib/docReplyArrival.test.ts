import { afterEach, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { createReplyArrival } from './docReplyArrival';
import type { DocComment } from './stores/docComments';

afterEach(() => { document.body.innerHTML = ''; vi.unstubAllGlobals(); });

it('waits for a late card to enter view and stops watching when the list closes', async () => {
	let notify: IntersectionObserverCallback | undefined;
	const observed = vi.fn(), disconnect = vi.fn();
	vi.stubGlobal('IntersectionObserver', class {
		constructor(callback: IntersectionObserverCallback) { notify = callback; }
		observe = observed;
		unobserve() {}
		disconnect = disconnect;
	});
	const list = document.createElement('div'); list.className = 'drawer-comments'; document.body.append(list);
	const mark = vi.fn().mockResolvedValue(undefined);
	const arrival = createReplyArrival(() => 'artifact', mark);
	const thread = { id: 'root', newReplyIds: ['reply'], replies: [{ id: 'reply' }] } as DocComment;
	arrival.whenShown(thread);
	await tick();
	expect(mark).not.toHaveBeenCalled();
	const card = document.createElement('div'); card.dataset.commentId = 'root'; list.append(card);
	await Promise.resolve();
	expect(observed).toHaveBeenCalledWith(card);
	notify!([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver);
	expect(mark).not.toHaveBeenCalled();
	notify!([{ isIntersecting: true, target: card } as IntersectionObserverEntry], {} as IntersectionObserver);
	expect(mark).toHaveBeenCalledWith('artifact', 'root', 'reply');
	expect(disconnect).toHaveBeenCalled();
	arrival.whenShown({ ...thread, newReplyIds: ['second'], replies: [{ id: 'second' }] } as DocComment);
	await tick();
	list.remove(); await Promise.resolve();
	expect(disconnect).toHaveBeenCalledTimes(2);
});
