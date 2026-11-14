import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread from './CommentThread.svelte';
import { pickedComments } from '$lib/stores/docComments';
import { get } from 'svelte/store';

let component: Record<string, unknown> | undefined;
afterEach(() => { if (component) unmount(component); component = undefined; pickedComments.set(new Set()); document.body.innerHTML = ''; vi.unstubAllGlobals(); });

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

it('stamps a picked thread at once, with no wait for it to come into view, and only once', async () => {
	vi.stubGlobal('IntersectionObserver', undefined);
	const host = document.createElement('div'); document.body.append(host);
	const onViewed = vi.fn().mockResolvedValue(undefined);
	component = mount(CommentThread, { target: host, props: { comment: { id: 'c', anchor: '', author: 'agent', text: 'note', at: '', resolved: false, replies: [], newComment: true }, onViewed } });
	flushSync();
	expect(onViewed).not.toHaveBeenCalled();
	pickedComments.set(new Set(['other']));
	flushSync();
	expect(onViewed).not.toHaveBeenCalled();
	pickedComments.set(new Set(['c']));
	flushSync();
	await Promise.resolve();
	expect(onViewed).toHaveBeenCalledTimes(1);
	expect(onViewed).toHaveBeenCalledWith('c', 'c');
	expect(get(pickedComments).size).toBe(0);
	document.dispatchEvent(new Event('visibilitychange')); flushSync();
	expect(onViewed).toHaveBeenCalledTimes(1);
});

it('stamps a thread picked before its card is drawn, once the card shows it', async () => {
	vi.stubGlobal('IntersectionObserver', undefined);
	pickedComments.set(new Set(['c']));
	const host = document.createElement('div'); document.body.append(host);
	const onViewed = vi.fn().mockResolvedValue(undefined);
	component = mount(CommentThread, { target: host, props: { comment: { id: 'c', anchor: '', author: 'agent', text: 'note', at: '', resolved: false, replies: [], newComment: true }, onViewed } });
	flushSync();
	await Promise.resolve();
	expect(onViewed).toHaveBeenCalledWith('c', 'c');
	expect(get(pickedComments).size).toBe(0);
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
