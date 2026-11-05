import { afterEach, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { createReplyArrival } from './docReplyArrival';
import type { DocComment } from './stores/docComments';

afterEach(() => { document.body.innerHTML = ''; vi.unstubAllGlobals(); });

const unread = { id: 'root', newReplyIds: ['reply'], replies: [{ id: 'reply' }] } as DocComment;

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
	let comments = [unread];
	const arrival = createReplyArrival(() => 'artifact', () => comments, mark);
	arrival.whenShown('root');
	await tick();
	expect(mark).not.toHaveBeenCalled();
	const card = document.createElement('div'); card.dataset.threadCard = 'root'; list.append(card);
	await Promise.resolve();
	expect(observed).toHaveBeenCalledWith(card);
	notify!([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver);
	expect(mark).not.toHaveBeenCalled();
	notify!([{ isIntersecting: true, target: card } as unknown as IntersectionObserverEntry], {} as IntersectionObserver);
	expect(mark).toHaveBeenCalledWith('artifact', 'root', 'reply');
	expect(disconnect).toHaveBeenCalled();
	comments = [{ ...unread, newReplyIds: ['second'], replies: [{ id: 'second' }] } as DocComment];
	arrival.whenShown('root');
	await tick();
	list.remove(); await Promise.resolve();
	expect(disconnect).toHaveBeenCalledTimes(2);
});

it('reads a card shown beside the text, never an image tag with the same id', async () => {
	let notify: IntersectionObserverCallback | undefined;
	const observed = vi.fn();
	vi.stubGlobal('IntersectionObserver', class {
		constructor(callback: IntersectionObserverCallback) { notify = callback; }
		observe = observed;
		unobserve() {}
		disconnect() {}
	});
	const popover = document.createElement('div'); popover.dataset.popover = ''; document.body.append(popover);
	const tag = document.createElement('button'); tag.dataset.commentId = 'root'; tag.dataset.regionTag = ''; popover.append(tag);
	const card = document.createElement('div'); card.dataset.threadCard = 'root'; popover.append(card);
	const mark = vi.fn().mockResolvedValue(undefined);
	createReplyArrival(() => 'artifact', () => [unread], mark).whenShown('root');
	await tick();
	expect(observed).toHaveBeenCalledWith(card);
	expect(observed).not.toHaveBeenCalledWith(tag);
	notify!([{ isIntersecting: true, target: card } as unknown as IntersectionObserverEntry], {} as IntersectionObserver);
	expect(mark).toHaveBeenCalledWith('artifact', 'root', 'reply');
});

it('reads a card whose surface opens after the arrival', async () => {
	let notify: IntersectionObserverCallback | undefined;
	const observed = vi.fn();
	vi.stubGlobal('IntersectionObserver', class {
		constructor(callback: IntersectionObserverCallback) { notify = callback; }
		observe = observed;
		unobserve() {}
		disconnect() {}
	});
	const mark = vi.fn().mockResolvedValue(undefined);
	createReplyArrival(() => 'artifact', () => [unread], mark).whenShown('root');
	await tick();
	const popover = document.createElement('div'); popover.dataset.popover = '';
	const card = document.createElement('div'); card.dataset.threadCard = 'root'; popover.append(card);
	document.body.append(popover);
	await Promise.resolve();
	expect(observed).toHaveBeenCalledWith(card);
	notify!([{ isIntersecting: true, target: card } as unknown as IntersectionObserverEntry], {} as IntersectionObserver);
	expect(mark).toHaveBeenCalledWith('artifact', 'root', 'reply');
});

it('stops waiting when the viewer goes away, before or after a surface opens', async () => {
	const observe = vi.fn(), disconnect = vi.fn();
	vi.stubGlobal('IntersectionObserver', class { observe = observe; unobserve() {} disconnect = disconnect; });
	const mark = vi.fn().mockResolvedValue(undefined);
	const early = createReplyArrival(() => 'artifact', () => [unread], mark);
	early.whenShown('root');
	early.stop();
	await tick();
	expect(disconnect).not.toHaveBeenCalled();
	const late = createReplyArrival(() => 'artifact', () => [unread], mark);
	late.whenShown('root');
	late.whenShown('gone');
	await tick();
	late.stop();
	expect(disconnect).toHaveBeenCalledTimes(1);
	const popover = document.createElement('div'); popover.dataset.popover = '';
	const card = document.createElement('div'); card.dataset.threadCard = 'root'; popover.append(card);
	document.body.append(popover);
	await Promise.resolve();
	expect(observe).not.toHaveBeenCalled();
});
