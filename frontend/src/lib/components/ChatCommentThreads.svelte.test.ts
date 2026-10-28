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

it('stamps each visible unread answer, including a tall answer filling the viewport', async () => {
	vi.useFakeTimers();
	class Observer {
		static all: Observer[] = [];
		target?: Element;
		constructor(private callback: IntersectionObserverCallback) { Observer.all.push(this); }
		observe(target: Element) { this.target = target; }
		disconnect() { this.target = undefined; }
		report(visible: boolean, tall = false) { if (this.target) this.callback([{ target: this.target, isIntersecting: visible, intersectionRatio: visible ? (tall ? 0.5 : 1) : 0, rootBounds: new DOMRect(0, 0, 300, 400), boundingClientRect: new DOMRect(0, 0, 300, 800), intersectionRect: new DOMRect(0, 0, 300, visible ? 400 : 0) } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
	}
	vi.stubGlobal('IntersectionObserver', Observer);
	const fetcher = vi.fn().mockResolvedValue({ ok: true });
	vi.stubGlobal('fetch', fetcher);
	Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
	host = document.createElement('div'); document.body.appendChild(host);
	const base = { from_off: 0, to_off: 1, anchor_prefix: '', anchor_suffix: '', id: 'root', cid: 'root', user_message_id: 'first', quote: 'quote', note: 'note', created_at: '2026-10-27T00:00:00Z' };
	component = mount(ChatCommentThreads, { target: host, props: { messageId: 'target', threadId: 'thread', comments: [{ ...base, answers: [
		{ author_kind: 'agent', user_message_id: 'first', cid: 'one', assistant_message_id: 'same', text: 'first answer', created_at: '2026-10-27T00:01:00Z', unread: true },
		{ author_kind: 'agent', user_message_id: 'second', cid: 'two', assistant_message_id: 'same', text: 'second answer', created_at: '2026-10-27T00:02:00Z', unread: true },
		{ author_kind: 'user', user_message_id: 'third', cid: 'three', text: 'user reply', created_at: '2026-10-27T00:03:00Z', unread: false },
	] }] } });
	flushSync();
	const rows = [...host.querySelectorAll<HTMLElement>('.ct-reply')];
	expect(rows).toHaveLength(3);
	expect(rows[0].dataset.replyId).not.toBe(rows[1].dataset.replyId);
	expect(rows[2].textContent).toContain('user reply');
	const first = Observer.all.find(item => item.target === rows[0])!;
	const second = Observer.all.find(item => item.target === rows[1])!;
	first.report(true, true); second.report(false);
	await vi.advanceTimersByTimeAsync(1499);
	expect(fetcher).not.toHaveBeenCalled();
	await vi.advanceTimersByTimeAsync(1);
	expect(fetcher).toHaveBeenCalledTimes(1);
	expect(fetcher.mock.calls[0][0]).toContain('/messages/first/comments/one/viewed');
	expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ assistant_message_id: 'same' });
	second.report(true);
	await vi.advanceTimersByTimeAsync(1500);
	expect(fetcher).toHaveBeenCalledTimes(2);
	expect(fetcher.mock.calls[1][0]).toContain('/messages/second/comments/two/viewed');
	delete (document as unknown as Record<string, unknown>).visibilityState;
	vi.useRealTimers();
});
