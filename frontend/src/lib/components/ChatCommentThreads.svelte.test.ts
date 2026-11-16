import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import ChatCommentThreads from './ChatCommentThreads.svelte';
import { chatPickKey, pickedComments } from '$lib/stores/docComments';

vi.mock('$lib/stores/attentionDock.svelte', () => ({ reload: vi.fn().mockResolvedValue(undefined) }));
const openAtNewest = vi.hoisted(() => vi.fn());
vi.mock('./comments/openAtNewestReply', () => ({ openAtNewestReply: openAtNewest }));

let host: HTMLElement;
let component: Record<string, unknown>;
afterEach(() => { openAtNewest.mockClear(); if (component) unmount(component); host?.remove(); vi.unstubAllGlobals(); });

it('shows a comment and its shared answer in the thread, and leaves an unanswered comment without a New mark', () => {
	host = document.createElement('div'); document.body.appendChild(host);
	const base = { from_off: 0, to_off: 1, anchor_prefix: '', anchor_suffix: '', user_message_id: 'send', created_at: '2026-10-27T00:00:00Z', note: 'Why?', quote: 'selected text' };
	component = mount(ChatCommentThreads, { target: host, props: {
		messageId: 'target', threadId: 'thread', comments: [
			{ ...base, id: 'one', cid: 'c1.1', answers: [{ author_kind: 'agent', user_message_id: 'send', cid: 'c1.1', assistant_message_id: 'answer', text: 'Shared answer', created_at: '2026-10-27T00:01:00Z', unread: true }] },
			{ ...base, id: 'two', cid: 'c1.2', answers: [] },
		],
	} });
	flushSync();
	const threads = host.querySelectorAll('[data-chat-comment]');
	expect(threads).toHaveLength(2);
	expect(threads[0].textContent).toContain('Shared answer');
	expect(threads[0].textContent).toContain('New');
	expect(threads[1].textContent).not.toContain('Shared answer');
	expect(threads[1].textContent).not.toContain('New');
});

it('labels a detached chat comment on its card and keeps its unread reply', () => {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(ChatCommentThreads, { target: host, props: {
		messageId: 'target', threadId: 'thread', detached: true,
		comments: [{ id: 'one', cid: 'c1.1', from_off: 0, to_off: 1, anchor_prefix: '', anchor_suffix: '', user_message_id: 'send', created_at: '2026-10-27T00:00:00Z', note: 'Why?', quote: 'gone', answers: [{ author_kind: 'agent', user_message_id: 'send', cid: 'c1.1', assistant_message_id: 'answer', text: 'Answer', created_at: '2026-10-27T00:01:00Z', unread: true }] }],
	} });
	flushSync();
	expect(host.textContent).toContain('This text can’t be highlighted in the message.');
	expect(host.textContent).toContain('New');
});

it('leaves unread answers unread while they are only on screen, and stamps each one when the thread is opened', async () => {
	vi.useFakeTimers();
	// Every row is reported fully in view: being seen must read nothing.
	vi.stubGlobal('IntersectionObserver', class {
		constructor(private callback: IntersectionObserverCallback) {}
		observe(target: Element) { this.callback([{ target, isIntersecting: true, intersectionRatio: 1 } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
		disconnect() {} unobserve() {}
	});
	const fetcher = vi.fn().mockResolvedValue({ ok: true });
	vi.stubGlobal('fetch', fetcher);
	Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
	host = document.createElement('div'); document.body.appendChild(host);
	const base = { from_off: 0, to_off: 1, anchor_prefix: '', anchor_suffix: '', id: 'root', cid: 'root', user_message_id: 'first', quote: 'quote', note: 'note', created_at: '2026-10-27T00:00:00Z' };
	const props = $state({ messageId: 'target', threadId: 'thread', opened: false, comments: [{ ...base, answers: [
		{ author_kind: 'agent' as const, user_message_id: 'first', cid: 'one', assistant_message_id: 'same', text: 'first answer', created_at: '2026-10-27T00:01:00Z', unread: true },
		{ author_kind: 'agent' as const, user_message_id: 'second', cid: 'two', assistant_message_id: 'same', text: 'second answer', created_at: '2026-10-27T00:02:00Z', unread: true },
		{ author_kind: 'user' as const, user_message_id: 'third', cid: 'three', text: 'user reply', created_at: '2026-10-27T00:03:00Z', unread: false },
	] }] });
	component = mount(ChatCommentThreads, { target: host, props });
	flushSync();
	const rows = [...host.querySelectorAll<HTMLElement>('.ct-reply')];
	expect(rows).toHaveLength(3);
	expect(rows[0].dataset.replyId).not.toBe(rows[1].dataset.replyId);
	expect(rows[2].textContent).toContain('user reply');
	await vi.advanceTimersByTimeAsync(5000);
	expect(fetcher).not.toHaveBeenCalled();
	props.opened = true;
	flushSync();
	await vi.advanceTimersByTimeAsync(1);
	expect(fetcher).toHaveBeenCalledTimes(2);
	expect(fetcher.mock.calls[0][0]).toContain('/messages/first/comments/one/viewed');
	expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ assistant_message_id: 'same' });
	expect(fetcher.mock.calls[1][0]).toContain('/messages/second/comments/two/viewed');
	delete (document as unknown as Record<string, unknown>).visibilityState;
	vi.useRealTimers();
});

const answered = (id: string) => ({ id, cid: `c-${id}`, from_off: 0, to_off: 1, anchor_prefix: '', anchor_suffix: '', user_message_id: 'send', created_at: '2026-10-27T00:00:00Z', note: `Note ${id}`, quote: 'same text', answers: [{ author_kind: 'agent', user_message_id: 'send', cid: `c-${id}`, assistant_message_id: `a-${id}`, text: `Answer ${id}`, created_at: '2026-10-27T00:01:00Z', unread: false }] });

it('opens a thread on its own at its newest reply, and a list of threads scrolls to none of them', () => {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(ChatCommentThreads, { target: host, props: { messageId: 'target', threadId: 'thread', comments: [answered('one')], onReply: async () => {} } });
	flushSync();
	expect(openAtNewest).toHaveBeenCalledTimes(1);
	unmount(component); openAtNewest.mockClear();
	component = mount(ChatCommentThreads, { target: host, props: { messageId: 'target', threadId: 'thread', comments: [answered('one'), answered('two')], listed: true } });
	flushSync();
	expect(host.querySelectorAll('[data-chat-comment]')).toHaveLength(2);
	expect(openAtNewest).not.toHaveBeenCalled();
});

it('reads an answer that arrived while its margin card stayed picked at the next press on its highlight', async () => {
	const fetcher = vi.fn().mockResolvedValue({ ok: true });
	vi.stubGlobal('fetch', fetcher);
	Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
	host = document.createElement('div'); document.body.appendChild(host);
	const base = { from_off: 0, to_off: 1, anchor_prefix: '', anchor_suffix: '', id: 'root', cid: 'root', user_message_id: 'first', quote: 'quote', note: 'note', created_at: '2026-10-27T00:00:00Z' };
	const read = { author_kind: 'agent' as const, user_message_id: 'first', cid: 'one', assistant_message_id: 'a1', text: 'first answer', created_at: '2026-10-27T00:01:00Z', unread: false };
	const props = $state({ messageId: 'target', threadId: 'thread', margin: true, picked: true, comments: [{ ...base, answers: [read] }] });
	component = mount(ChatCommentThreads, { target: host, props });
	flushSync();
	// An answer arrives while the card stays picked: arriving reads nothing.
	props.comments = [{ ...base, answers: [read, { ...read, user_message_id: 'second', cid: 'two', assistant_message_id: 'a2', text: 'second answer', created_at: '2026-10-27T00:02:00Z', unread: true }] }];
	flushSync();
	await Promise.resolve();
	expect(fetcher).not.toHaveBeenCalled();
	// A press on its highlight (ChatCommentSelection) picks the card again, which reads it, as a document highlight press does.
	pickedComments.set(new Set([chatPickKey('root')]));
	flushSync();
	await Promise.resolve();
	expect(fetcher).toHaveBeenCalledTimes(1);
	expect(fetcher.mock.calls[0][0]).toContain('/messages/second/comments/two/viewed');
	expect(get(pickedComments).size).toBe(0);
	delete (document as unknown as Record<string, unknown>).visibilityState;
});
