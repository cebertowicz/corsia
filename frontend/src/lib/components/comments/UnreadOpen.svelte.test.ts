import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread from './CommentThread.svelte';

let component: Record<string, unknown> | undefined;
afterEach(() => { if (component) unmount(component); document.body.innerHTML = ''; vi.unstubAllGlobals(); });

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
