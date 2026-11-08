import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { messages } from '$lib/stores/threads';
import ChatMarginColumn from './ChatMarginColumn.svelte';
import ChatCommentThreads from './ChatCommentThreads.svelte';
import { chatReplyDrafts } from './chatReplyDrafts.svelte';

vi.mock('$lib/stores/attentionDock.svelte', () => ({ reload: vi.fn().mockResolvedValue(undefined) }));

let host: HTMLElement;
let clip: HTMLElement;
let scroller: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

/** A transcript row whose comment is painted, and one whose text is gone so nothing is painted. */
function transcript() {
	host = document.createElement('div');
	scroller = document.createElement('div');
	scroller.className = 'messages';
	clip = document.createElement('div');
	clip.className = 'chat-margin-clip';
	scroller.innerHTML = `<div class="message" data-message-id="m1"><div class="long-message-body"><span data-comment-ids="kept" role="button">runs at 2 AM</span> and copies the workspace.</div></div>`;
	host.append(scroller, clip);
	document.body.append(host);
	const at = (el: HTMLElement, top: number) => { el.getBoundingClientRect = () => ({ top, bottom: top + 20, left: 0, right: 100, width: 100, height: 20, x: 0, y: top, toJSON: () => ({}) }) as DOMRect; };
	at(scroller.querySelector<HTMLElement>('[data-comment-ids]')!, 120);
	at(scroller.querySelector<HTMLElement>('.long-message-body')!, 300);
	at(clip, 0);
}

function thread() {
	messages.set([{
		id: 'm1', thread_id: 'thread', role: 'assistant', content: 'runs at 2 AM and copies the workspace.', seq: 1, created_at: '2026-11-08T00:00:00Z',
		chat_comments: [
			{ id: 'kept', cid: 'c1', user_message_id: 's1', quote: 'runs at 2 AM', note: 'Why 2 AM?', from_off: 0, to_off: 12, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-08T00:00:00Z', answers: [] },
			{ id: 'lost', cid: 'c2', user_message_id: 's1', quote: 'external drive', note: 'Which drive?', from_off: 40, to_off: 54, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-08T00:00:00Z', answers: [] },
		],
	} as never]);
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	messages.set([]);
	host?.remove();
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
});

it('puts one card per comment of the thread in the column, each beside where its comment is drawn', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	transcript(); thread();
	const picks: string[] = [];
	component = mount(ChatMarginColumn, { target: clip, props: { threadId: 'thread', scroller, selectedId: null, onSelect: (id: string) => picks.push(id), onSendReply: vi.fn() } });
	flushSync();

	const cards = clip.querySelectorAll<HTMLElement>('[data-margin-card]');
	expect([...cards].map(card => card.dataset.marginCard)).toEqual(['kept', 'lost']);
	// The painted highlight holds its card; the comment whose text is gone is placed at the message body its text left.
	expect(cards[0].style.top).toBe('120px');
	expect(cards[1].style.top).toBe('300px');
	expect(cards[0].textContent).toContain('Why 2 AM?');
	expect(cards[1].textContent).toContain('Which drive?');
	expect(clip.querySelectorAll('.chat-comment-threads.margin')).toHaveLength(2);

	cards[1].querySelector<HTMLElement>('.chat-margin-card')!.click();
	expect(picks).toEqual(['lost']);
});

it('lists only this thread\'s comments and rides the transcript it is given', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	transcript(); thread();
	messages.update(current => [...current, { id: 'm2', thread_id: 'other', role: 'assistant', content: 'elsewhere', seq: 2, created_at: '2026-11-08T00:00:00Z', chat_comments: [{ id: 'else', cid: 'c9', user_message_id: 's9', quote: 'elsewhere', note: 'No', from_off: 0, to_off: 9, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-08T00:00:00Z', answers: [] }] } as never]);
	const sent: unknown[] = [];
	component = mount(ChatMarginColumn, { target: clip, props: { threadId: 'thread', scroller, selectedId: null, onSelect: () => {}, onSendReply: (reply: unknown) => sent.push(reply) } });
	flushSync();

	expect(clip.querySelectorAll('[data-margin-card]')).toHaveLength(2);
	// The reply carries the comment's own anchor, so it answers the same text the card quotes.
	clip.querySelectorAll<HTMLElement>('[data-margin-card]')[0].querySelector<HTMLTextAreaElement>('textarea')!.value = 'Please change it';
	clip.querySelectorAll<HTMLElement>('[data-margin-card]')[0].querySelector<HTMLTextAreaElement>('textarea')!.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	clip.querySelectorAll<HTMLElement>('[data-margin-card]')[0].querySelector<HTMLElement>('[data-send-reply]')!.click();
	flushSync();
	expect(sent).toHaveLength(1);
	expect(sent[0]).toMatchObject({ messageId: 'm1', quote: 'runs at 2 AM', note: 'Please change it', root_user_message_id: 's1', root_cid: 'c1' });
});

it('keeps an unsent reply when the canvas narrows and the same thread opens in its popover', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	transcript(); thread();
	component = mount(ChatMarginColumn, { target: clip, props: { threadId: 'thread', scroller, selectedId: null, onSelect: () => {}, onSendReply: vi.fn() } });
	flushSync();
	const field = clip.querySelectorAll<HTMLElement>('[data-margin-card]')[1].querySelector<HTMLTextAreaElement>('textarea')!;
	field.value = 'The one in the closet';
	field.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();

	// Narrowing unmounts the column; the popover shows the same thread on its own.
	unmount(component);
	const popover = document.createElement('div');
	document.body.append(popover);
	const lost = get(messages)[0].chat_comments!.find(comment => comment.id === 'lost')!;
	component = mount(ChatCommentThreads, { target: popover, props: { messageId: 'm1', threadId: 'thread', comments: [lost], onReply: vi.fn() } });
	flushSync();
	expect(popover.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('The one in the closet');
	chatReplyDrafts.set('lost', '');
});
