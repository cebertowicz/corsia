import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread from './CommentThread.svelte';
import type { CommentThreadData } from './CommentThreadData';

const LONG = 'A long comment body. '.repeat(50);
let component: Record<string, unknown> | null = null;
let host: HTMLElement;
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); vi.unstubAllGlobals(); });

const bodies = () => [...host.querySelectorAll<HTMLElement>('.ct-markdown-reveal')];
const whole = () => bodies().map((body) => body.classList.contains('whole'));

function mountThread(comment: CommentThreadData) {
	vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
	host = document.createElement('div'); document.body.append(host);
	component = mount(CommentThread, { target: host, props: { comment, onViewed: vi.fn().mockResolvedValue(undefined) } });
	flushSync();
}

it('keeps a long comment and reply whole once they were unread, after New clears and after the resolved body remounts', () => {
	const state = $state<{ comment: CommentThreadData }>({ comment: {
		id: 'c', anchor: 'quote', author: 'agent', text: LONG, at: 'now', resolved: true, newComment: true,
		replies: [{ id: 'r', author: 'agent', text: LONG, at: 'now' }], newReplyIds: ['r']
	} });
	mountThread(state.comment);
	expect(whole()).toEqual([true, true]);

	// The viewed stamp lands: the server clears both New marks while the person is reading.
	state.comment.newComment = false;
	state.comment.newReplyIds = [];
	flushSync();
	expect(whole()).toEqual([true, true]);

	// Closing and reopening the resolved card remounts its bodies; the thread still holds them unread.
	host.querySelector<HTMLButtonElement>('.ct-res-head')!.click(); flushSync();
	host.querySelector<HTMLButtonElement>('.ct-res-head')!.click(); flushSync();
	expect(bodies()).toHaveLength(2);
	expect(whole()).toEqual([true, true]);
});

const newMarks = () => host.querySelectorAll('.ct-new').length;

for (const chatSurface of [false, true]) it(`holds the comment's and reply's New marks while the ${chatSurface ? 'chat' : 'document'} thread is open, and lets them go once it rests`, () => {
	vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
	const props = $state({
		comment: { id: 'c', anchor: 'quote', author: 'agent', text: 'Short note.', at: 'now', resolved: false, newComment: true, replies: [{ id: 'r', author: 'agent', text: 'Short answer.', at: 'now' }], newReplyIds: ['r'] } as CommentThreadData,
		chatSurface, opened: chatSurface, hideReply: false, onViewed: vi.fn().mockResolvedValue(undefined)
	});
	host = document.createElement('div'); document.body.append(host);
	component = mount(CommentThread, { target: host, props });
	flushSync();
	expect(newMarks()).toBe(2);

	// The viewed stamp lands while the thread is open: the server clears both, the marks stay under the reader.
	props.comment.newComment = false;
	props.comment.newReplyIds = [];
	flushSync();
	expect(newMarks()).toBe(2);

	// The thread rests (a chat thread closed, a document card back at its preview): the marks go.
	if (chatSurface) props.opened = false; else props.hideReply = true;
	flushSync();
	expect(newMarks()).toBe(0);
});

it('collapses a long comment and reply that were already read when the thread mounted', () => {
	mountThread({ id: 'c', anchor: 'quote', author: 'agent', text: LONG, at: 'now', resolved: false, replies: [{ id: 'r', author: 'agent', text: LONG, at: 'now' }] });
	expect(whole()).toEqual([false, false]);
});
