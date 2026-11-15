import { get } from 'svelte/store';
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import AssistantMessage from './AssistantMessage.svelte';
import ChatCommentSelection from './ChatCommentSelection.svelte';
import { messages } from '$lib/stores/threads';

// A comment is anchored to the text it was made on. When that text leaves the message — an edited or
// retried answer — no highlight can carry the comment, and the comment would have no surface at all.
// The highlight pass records what it could not place, and the message row shows those comments beneath
// itself. These run the two components together, because that is the only way to see the hand-off.

const CONTENT = 'Kept text that the comment no longer matches';
const AGENT_REPLY = { author_kind: 'agent', user_message_id: 'agent-message', cid: 'c1', assistant_message_id: 'same', text: 'The answer', created_at: '2026-11-05T00:01:00Z', unread: true };

function comment(quote: string) {
	return {
		id: 'one', cid: 'c1', user_message_id: 'user-message', quote, note: 'Why?',
		from_off: 0, to_off: quote.length, anchor_prefix: '', anchor_suffix: '',
		created_at: '2026-11-05T00:00:00Z', answers: [{ ...AGENT_REPLY }],
	};
}

let host: HTMLElement;
let components: unknown[] = [];

/** The transcript row and the highlight surface, mounted the way the chat mounts them. */
function mountTranscript(quote: string, onSendReply = vi.fn(), activityFirst = false) {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	host = document.createElement('div');
	// Focus mode can draw a message's activity line, which carries the same message id, above its text row.
	const activity = activityFirst ? '<div class="block-line" data-message-id="m1"></div>' : '';
	host.innerHTML = `<div class="messages">${activity}<div class="message assistant" data-message-id="m1"><div id="message"></div></div></div><div id="mount"></div>`;
	document.body.append(host);
	const row = { id: 'm1', seq: 5, thread_id: 'thread', role: 'assistant', content: CONTENT, chat_comments: [comment(quote)] };
	messages.set([row as never]);
	components = [
		mount(AssistantMessage, {
			target: host.querySelector('#message')!,
			props: { msg: row, expandedTools: new Set<string>(), expandedReasoning: new Set<string>(), onToggleToolBlock: () => {}, onToggleReasoning: () => {} },
		}),
		mount(ChatCommentSelection, { target: host.querySelector('#mount')!, props: { threadId: 'thread', onSendReply } }),
	];
	flushSync();
	return {
		row: () => host.querySelector<HTMLElement>('.message[data-message-id="m1"]')!,
		body: () => host.querySelector<HTMLElement>('.message[data-message-id="m1"] .long-message-body')!,
	};
}

/** Replying from a detached thread has to behave like replying from a highlight: the same thread, the same immediate send. */
async function replyFromDetachedThread(onSendReply: Parameters<typeof mountTranscript>[1], activityFirst = false, send = true) {
	const { row } = mountTranscript('Gone text', onSendReply, activityFirst);
	await vi.waitFor(() => expect(row()?.querySelector('[data-reply-comment="one"]')).not.toBeNull());
	row()!.querySelector<HTMLElement>('[data-reply-comment="one"]')!.click();
	flushSync();
	// The thread popover is portaled to document.body, like every other popover.
	const dialog = document.body.querySelector<HTMLElement>('[role="dialog"][aria-label="Comment"]');
	const field = dialog?.querySelector<HTMLTextAreaElement>('.ct-input');
	expect(field, 'the click opens that comment\u2019s thread with its reply field').toBeTruthy();
	// jsdom lets a click through a hidden, clipped surface, so check the surface is shown: it has no highlight to sit on.
	await vi.waitFor(() => expect(dialog!.closest('[data-popover]')?.getAttribute('aria-hidden'), 'the thread is shown, not hidden for want of a highlight').toBeNull());
	field!.value = 'Because it changed';
	field!.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	if (!send) return;
	dialog!.querySelector<HTMLElement>('[data-send-reply]')!.click();
	flushSync();
}

afterEach(() => {
	for (const component of components) unmount(component as never);
	components = [];
	messages.set([]);
	host?.remove();
	vi.unstubAllGlobals();
	vi.useRealTimers();
});

it('shows a comment whose text left the message as a thread under the message, with its reply reachable', async () => {
	const { row } = mountTranscript('Gone text');
	await vi.waitFor(() => expect(row()?.querySelector('[data-comment-uuid="one"]')?.textContent).toContain('The answer'));
	// One surface, not two: nothing is highlighted, and what is shown is the thread itself.
	expect(row()!.querySelector('.long-message-body [data-comment-ids]')).toBeNull();
	// Reachable means reachable: the reply control is on the thread, so the person can answer the comment.
	expect(row()!.querySelector('[data-reply-comment="one"]')).not.toBeNull();
	// The chat surface names its own state: the text is here, only the highlight was lost.
	expect(row()!.querySelector('[data-comment-uuid="one"] [data-detached]')?.textContent)
		.toBe('This text can’t be highlighted in the message.');
});

it('replies from the detached thread on the same path the popover uses', async () => {
	const onSendReply = vi.fn();
	await replyFromDetachedThread(onSendReply);
	expect(onSendReply).toHaveBeenCalledTimes(1);
	const sent = onSendReply.mock.calls[0][0];
	expect(sent).toMatchObject({ messageId: 'm1', seq: 5, quote: 'Gone text', note: 'Because it changed', root_cid: 'c1', root_user_message_id: 'user-message' });
});

it('shows the detached thread when the message’s activity line is drawn above its text', async () => {
	const onSendReply = vi.fn();
	await replyFromDetachedThread(onSendReply, true);
	expect(onSendReply).toHaveBeenCalledTimes(1);
});

it('keeps an unsent reply when the window narrows from the popover to the drawer', async () => {
	vi.stubGlobal('innerWidth', 1440);
	await replyFromDetachedThread(vi.fn(), false, false);
	vi.stubGlobal('innerWidth', 500);
	window.dispatchEvent(new Event('resize'));
	flushSync();
	await tick();
	const field = document.body.querySelector<HTMLTextAreaElement>('[data-adaptive-drawer] .ct-input');
	expect(field, 'the drawer replaces the popover').toBeTruthy();
	expect(field!.value).toBe('Because it changed');
});

it('leaves a comment whose text is still in the message on its highlight alone', async () => {
	const { row, body } = mountTranscript('Gone text');
	// Anchor the comment in the rendered text the way a selection does, then let the paint pass place it.
	const text = body()!.textContent ?? '';
	const from = text.indexOf('Kept text');
	expect(from).toBeGreaterThanOrEqual(0);
	messages.update(current => [{ ...current[0]!, chat_comments: [{ ...comment('Kept text'), from_off: from, to_off: from + 'Kept text'.length }] }] as never);
	await vi.waitFor(() => expect(row()?.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	expect(row()!.querySelector('[data-comment-uuid="one"]')).toBeNull();
	// Its name is a sentence with the quote set off, so a screen reader hears where the quoted text ends.
	await vi.waitFor(() => expect(row()!.querySelector('.long-message-body [data-comment-ids="one"]')?.getAttribute('aria-label'))
		.toBe('The comment on “Kept text” has a new reply.'));
});

it('does not repaint the highlights when a detached thread appears under the message', async () => {
	const { row } = mountTranscript('Gone text');
	await vi.waitFor(() => expect(row()?.querySelector('[data-comment-uuid="one"]')?.textContent).toContain('The answer'));
	// Adding the thread mutates the row, so this is where a paint pass would start one it cannot finish.
	await new Promise<void>(resolve => setTimeout(resolve, 300));
	const frames = vi.spyOn(window, 'requestAnimationFrame');
	await new Promise<void>(resolve => setTimeout(resolve, 120));
	expect(frames).not.toHaveBeenCalled();
	expect(row()!.querySelectorAll('[data-comment-uuid="one"]')).toHaveLength(1);
	frames.mockRestore();
});

it('keeps a detached thread’s answer unread while it is on screen, and clears it once the person opens the thread', async () => {
	vi.useFakeTimers();
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	// Every observed element is reported fully in view: being seen must read nothing.
	vi.stubGlobal('IntersectionObserver', class {
		constructor(private callback: IntersectionObserverCallback) {}
		observe(target: Element) { this.callback([{ target, isIntersecting: true, intersectionRatio: 1 } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
		disconnect() {} unobserve() {}
	});
	const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ threads: [] }) });
	vi.stubGlobal('fetch', fetcher);
	Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
	const { row } = mountTranscript('Gone text');
	await tick();
	await vi.advanceTimersByTimeAsync(0);
	flushSync();
	const card = row()!.querySelector<HTMLElement>('[data-comment-uuid="one"] .ct');
	expect(card?.querySelector('.ct-reply')).not.toBeNull();
	const stamped = () => fetcher.mock.calls.some(call => String(call[0]).includes('/messages/agent-message/comments/c1/viewed'));
	await vi.advanceTimersByTimeAsync(5000);
	expect(stamped()).toBe(false);
	expect(get(messages)[0]!.chat_comments?.[0]?.answers[0]?.unread).toBe(true);
	card!.click();
	await vi.advanceTimersByTimeAsync(1);
	expect(stamped()).toBe(true);
	expect(get(messages)[0]!.chat_comments?.[0]?.answers[0]?.unread).toBe(false);
	delete (document as unknown as Record<string, unknown>).visibilityState;
});
