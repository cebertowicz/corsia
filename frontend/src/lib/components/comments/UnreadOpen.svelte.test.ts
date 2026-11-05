import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread from './CommentThread.svelte';
import { arrivedDocComments } from '$lib/stores/docComments';
import { get } from 'svelte/store';

let component: Record<string, unknown> | undefined;
afterEach(() => { if (component) unmount(component); component = undefined; arrivedDocComments.set(new Set()); document.body.innerHTML = ''; vi.unstubAllGlobals(); });

it('keeps a resolved unread thread expanded until it is opened', async () => {
	vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
	const host = document.createElement('div'); document.body.append(host);
	const onViewed = vi.fn().mockResolvedValue(undefined);
	component = mount(CommentThread, { target: host, props: {
		comment: { id: 'c', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: true,
			replies: [{ id: 'r', author: 'agent', text: 'reply', at: 'now' }], newReplyIds: ['r'] }, onViewed
	} });
	flushSync();
	expect(host.textContent).toContain('reply');
	expect(host.textContent).toContain('New');
	expect(onViewed).not.toHaveBeenCalled();
	host.querySelector<HTMLElement>('.ct')!.click();
	await Promise.resolve();
	expect(onViewed).toHaveBeenCalledWith('c', 'r');
});

it('marks an unread top-level comment only when its card is opened', async () => {
	vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
	const host = document.createElement('div'); document.body.append(host);
	const onViewed = vi.fn().mockResolvedValue(undefined);
	component = mount(CommentThread, { target: host, props: {
		comment: { id: 'root', anchor: 'quote', author: 'agent', text: 'A new comment', at: 'now', resolved: false, replies: [], newComment: true }, onViewed
	} });
	flushSync();
	expect(host.textContent).toContain('New');
	expect(onViewed).not.toHaveBeenCalled();
	host.querySelector<HTMLElement>('.ct')!.click();
	await Promise.resolve();
	expect(onViewed).toHaveBeenCalledWith('root', 'root');
});

it('stamps a chip arrival only after its card becomes visible, independent of other surfaces', async () => {
	let visible!: () => void;
	vi.stubGlobal('IntersectionObserver', class {
		constructor(private callback: IntersectionObserverCallback) {}
		observe(target: Element) { visible = () => this.callback([{ target, isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
		disconnect() {} unobserve() {}
	});
	const host = document.createElement('div'); document.body.append(host);
	const onViewed = vi.fn().mockResolvedValue(undefined);
	component = mount(CommentThread, { target: host, props: { comment: { id: 'c', anchor: '', author: 'agent', text: 'note', at: '', resolved: false, replies: [], newComment: true }, onViewed } });
	flushSync();
	arrivedDocComments.set(new Set(['c']));
	const unrelated = document.createElement('div'); unrelated.setAttribute('data-popover', ''); document.body.append(unrelated); unrelated.remove();
	flushSync();
	expect(onViewed).not.toHaveBeenCalled();
	visible(); flushSync();
	await Promise.resolve();
	expect(onViewed).toHaveBeenCalledTimes(1);
	expect(onViewed).toHaveBeenCalledWith('c', 'c');
	expect(get(arrivedDocComments).size).toBe(0);
	visible(); flushSync();
	expect(onViewed).toHaveBeenCalledTimes(1);
});

it('keeps an arrival unread without an observer until the card is opened', async () => {
	vi.stubGlobal('IntersectionObserver', undefined);
	arrivedDocComments.set(new Set(['c']));
	const host = document.createElement('div'); document.body.append(host);
	const onViewed = vi.fn().mockResolvedValue(undefined);
	component = mount(CommentThread, { target: host, props: { comment: { id: 'c', anchor: '', author: 'agent', text: 'note', at: '', resolved: false, replies: [], newComment: true }, onViewed } });
	flushSync();
	expect(onViewed).not.toHaveBeenCalled();
	host.querySelector<HTMLElement>('.ct')!.click();
	await Promise.resolve();
	expect(onViewed).toHaveBeenCalledWith('c', 'c');
});

it('retries a failed stamp on the next opening', async () => {
	vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} });
	const host = document.createElement('div'); document.body.append(host);
	const onViewed = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(undefined);
	const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
	component = mount(CommentThread, { target: host, props: { comment: { id: 'c', anchor: '', author: 'agent', text: 'note', at: '', resolved: false, replies: [], newComment: true }, onViewed } });
	flushSync();
	host.querySelector<HTMLElement>('.ct')!.click();
	await Promise.resolve(); await Promise.resolve();
	host.querySelector<HTMLElement>('.ct')!.click();
	expect(onViewed).toHaveBeenCalledTimes(2);
	warn.mockRestore();
});
