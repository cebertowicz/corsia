import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentThread from './CommentThread.svelte';
import type { CommentThreadData } from './CommentThreadData';

type Reply = NonNullable<CommentThreadData['replies']>[number];
const reply = (id: string, createdAt = '2026-10-16T18:00:00Z'): Reply => ({ id, author: 'agent-1', authorKind: 'agent', text: `reply ${id}`, at: 'later', createdAt });
const state = $state<{ comment: CommentThreadData }>({ comment: { id: 'c1', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] } });

/** An observer that reports everything it observes as fully on screen at once: being seen must read nothing. */
class SeenObserver {
	constructor(private callback: IntersectionObserverCallback) {}
	observe(target: Element) {
		this.callback([{ isIntersecting: true, intersectionRatio: 1, target } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
	}
	disconnect() {}
	unobserve() {}
	takeRecords() { return []; }
	root = null; rootMargin = ''; thresholds = [];
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
/** How the caller presents the thread: a chat margin card picked or at rest, or a popover opened on it. */
const shown = $state({ opened: false, hideReply: false });
function render(onViewed: (id: string, replyId: string) => Promise<void>, props: Record<string, unknown> = {}) {
	vi.stubGlobal('IntersectionObserver', SeenObserver);
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(CommentThread, { target: host, props: { get comment() { return state.comment; }, onViewed, chatSurface: true, get opened() { return shown.opened; }, get hideReply() { return shown.hideReply; }, ...props } });
	flushSync();
}
function arrive(comment: Partial<CommentThreadData>) {
	state.comment = { ...state.comment, ...comment }; flushSync();
}
const newMarks = () => host.querySelectorAll('.ct-new').length;
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

afterEach(() => {
	if (component) unmount(component);
	component = null; host?.remove();
	shown.opened = false; shown.hideReply = false;
	state.comment = { id: 'c1', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] };
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('chat thread read stamps', () => {
	it('reads nothing when the thread loads and stays on screen', async () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1'), reply('r2')], newReplyIds: ['r1', 'r2'] };
		render(onViewed);
		// Well past any timer the thread runs, so a read on a delay would have landed.
		await wait(1_800);
		expect(onViewed).not.toHaveBeenCalled();
		expect(newMarks()).toBe(2);
	});

	it('reads nothing on a resting margin card, and reads every unread answer once the card is picked', () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r0'), reply('r1'), reply('r2')], newReplyIds: ['r1', 'r2'] };
		shown.hideReply = true;
		render(onViewed, { inMargin: true });
		expect(onViewed).not.toHaveBeenCalled();
		shown.hideReply = false; shown.opened = true; flushSync();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1'], ['c1', 'r2']]);
	});

	it('reads every unread answer when the thread mounts opened (a popover or sheet)', () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1'), reply('r2')], newReplyIds: ['r1', 'r2'] };
		shown.opened = true;
		render(onViewed);
		expect(onViewed.mock.calls).toEqual([['c1', 'r1'], ['c1', 'r2']]);
	});

	it('reads the unread answers when the card is pressed, or its reply field takes focus', () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		host.querySelector<HTMLElement>('[data-comment-card]')!.click(); flushSync();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1']]);
		arrive({ replies: [reply('r1'), reply('r2', '2026-10-16T18:01:00Z')], newReplyIds: ['r2'] });
		expect(onViewed).toHaveBeenCalledTimes(1);
		host.querySelector('textarea')!.dispatchEvent(new FocusEvent('focusin', { bubbles: true })); flushSync();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1'], ['c1', 'r2']]);
	});

	it('leaves an answer that arrives while the thread is open unread until the next press', () => {
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		shown.opened = true;
		render(onViewed);
		expect(onViewed.mock.calls).toEqual([['c1', 'r1']]);
		arrive({ replies: [reply('r1'), reply('r2', '2026-10-16T18:01:00Z')], newReplyIds: ['r2'] });
		expect(onViewed).toHaveBeenCalledTimes(1);
		host.querySelector<HTMLElement>('[data-comment-card]')!.click(); flushSync();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1'], ['c1', 'r2']]);
	});

	it('keeps the New marks the thread opened with after the stamp clears them, until it is no longer open', () => {
		state.comment = { ...state.comment, replies: [reply('r1'), reply('r2', '2026-10-16T18:01:00Z')], newReplyIds: ['r1', 'r2'] };
		shown.hideReply = true;
		render(vi.fn().mockResolvedValue(undefined), { inMargin: true });
		expect(newMarks()).toBe(2);
		shown.hideReply = false; shown.opened = true; flushSync();
		// The stamp lands: the server names no new answers, and the open card still shows which ones were new.
		arrive({ newReplyIds: [] });
		expect(newMarks()).toBe(2);
		shown.hideReply = true; shown.opened = false; flushSync();
		expect(newMarks()).toBe(0);
		shown.hideReply = false; shown.opened = true; flushSync();
		expect(newMarks()).toBe(0);
	});

	it('retries a failed stamp on the next opening, not in a loop', async () => {
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const onViewed = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		render(onViewed);
		host.querySelector<HTMLElement>('[data-comment-card]')!.click(); flushSync();
		await vi.waitFor(() => expect(warn).toHaveBeenCalled());
		await wait(50);
		expect(onViewed).toHaveBeenCalledTimes(1);
		host.querySelector<HTMLElement>('[data-comment-card]')!.click(); flushSync();
		expect(onViewed).toHaveBeenCalledTimes(2);
	});
});

describe('document thread stamps', () => {
	it('reads nothing when a selected image-region card focuses its own reply field, and reads once the person moves focus there', () => {
		const card = $state({ focused: false });
		const onViewed = vi.fn().mockResolvedValue(undefined);
		state.comment = { ...state.comment, replies: [reply('r1')], newReplyIds: ['r1'] };
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(CommentThread, { target: host, props: { get comment() { return state.comment; }, onViewed, selectable: true, drawerCard: true, scrollOnFocus: false, get focused() { return card.focused; } } });
		flushSync();
		// Selected by something other than a pick (a restored tab's landing): the card puts the caret in its field, and reads nothing.
		card.focused = true; flushSync();
		expect(document.activeElement).toBe(host.querySelector('textarea'));
		expect(onViewed).not.toHaveBeenCalled();
		// The person moving focus into the field opens the thread.
		host.querySelector('textarea')!.dispatchEvent(new FocusEvent('focusin', { bubbles: true })); flushSync();
		expect(onViewed.mock.calls).toEqual([['c1', 'r1']]);
	});

	it('keeps the New marks a picked document card opened with after the viewed stamp clears them, until the card rests again', () => {
		const card = $state({ resting: true });
		state.comment = { ...state.comment, replies: [reply('r1'), reply('r2', '2026-10-16T18:01:00Z')], newReplyIds: ['r1', 'r2'] };
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(CommentThread, { target: host, props: { get comment() { return state.comment; }, onViewed: vi.fn().mockResolvedValue(undefined), inMargin: true, get hideReply() { return card.resting; } } });
		flushSync();
		// At rest the preview carries one New mark for its unread replies.
		expect(newMarks()).toBe(1);
		card.resting = false; flushSync();
		expect(newMarks()).toBe(2);
		// The stamp lands: the server names no new replies, and the picked card still shows which ones were new.
		arrive({ newReplyIds: [] });
		expect(newMarks()).toBe(2);
		card.resting = true; flushSync();
		expect(newMarks()).toBe(0);
		card.resting = false; flushSync();
		expect(newMarks()).toBe(0);
	});
});
