import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ChatCommentThreads from './ChatCommentThreads.svelte';

vi.mock('$lib/stores/attentionDock.svelte', () => ({ reload: vi.fn().mockResolvedValue(undefined) }));

let host: HTMLElement;
let component: Record<string, unknown>;
afterEach(() => { if (component) unmount(component); host?.remove(); vi.unstubAllGlobals(); });

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
