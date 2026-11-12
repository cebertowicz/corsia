import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { messages } from '$lib/stores/threads';
import { toasts } from '$lib/stores/toast';
import ChatMarginColumn from './ChatMarginColumn.svelte';
import ChatCommentThreads from './ChatCommentThreads.svelte';
import { chatReplyDrafts } from './chatReplyDrafts.svelte';

vi.mock('$lib/stores/attentionDock.svelte', () => ({ reload: vi.fn().mockResolvedValue(undefined) }));

let host: HTMLElement;
let scroller: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

/** A transcript row whose comment is painted, and one whose text is gone so nothing is painted. */
function transcript() {
	host = document.createElement('div');
	scroller = document.createElement('div');
	scroller.className = 'messages';
	scroller.innerHTML = `<div class="message" data-message-id="m1"><div class="long-message-body"><span data-comment-ids="kept" role="button">runs at 2 AM</span> and copies the workspace.</div></div>`;
	host.append(scroller);
	document.body.append(host);
	const at = (el: HTMLElement, top: number) => { el.getBoundingClientRect = () => ({ top, bottom: top + 20, left: 0, right: 100, width: 100, height: 20, x: 0, y: top, toJSON: () => ({}) }) as DOMRect; };
	at(scroller.querySelector<HTMLElement>('[data-comment-ids]')!, 120);
	at(scroller.querySelector<HTMLElement>('.long-message-body')!, 300);
	at(scroller, 0);
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
	chatReplyDrafts.release('thread', 'test-input');
	chatReplyDrafts.set('kept', '');
	chatReplyDrafts.set('lost', '');
	messages.set([]);
	host?.remove();
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
});

it('puts one card per comment of the thread in the column, each beside where its comment is drawn', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	transcript(); thread();
	const picks: string[] = [];
	component = mount(ChatMarginColumn, { target: host, props: { threadId: 'thread', scroller, selectedId: null, onSelect: (id: string) => picks.push(id), onSendReply: vi.fn() } });
	flushSync();

	// The column is drawn on the canvas: inside the transcript's scroller, so its cards scroll with their messages.
	expect(scroller.querySelector('.chat-margin')?.classList.contains('on-canvas')).toBe(true);
	const cards = scroller.querySelectorAll<HTMLElement>('[data-margin-card]');
	expect([...cards].map(card => card.dataset.marginCard)).toEqual(['kept', 'lost']);
	// The painted highlight holds its card; the comment whose text is gone is placed at the message body its text left.
	expect(cards[0].style.top).toBe('120px');
	expect(cards[1].style.top).toBe('300px');
	expect(cards[0].textContent).toContain('Why 2 AM?');
	expect(cards[1].textContent).toContain('Which drive?');
	expect(host.querySelectorAll('.chat-comment-threads.margin')).toHaveLength(2);
	// A resting card is the thread alone: no quote, since it sits level with its text, and no Reply field until it is picked.
	expect(cards[0].textContent).not.toContain('“runs at 2 AM”');
	expect(cards[0].querySelector('textarea')).toBeNull();

	cards[1].querySelector<HTMLElement>('.chat-margin-card')!.click();
	expect(picks).toEqual(['lost']);
});

it('lists only this thread\'s comments and rides the transcript it is given', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	transcript(); thread();
	messages.update(current => [...current, { id: 'm2', thread_id: 'other', role: 'assistant', content: 'elsewhere', seq: 2, created_at: '2026-11-08T00:00:00Z', chat_comments: [{ id: 'else', cid: 'c9', user_message_id: 's9', quote: 'elsewhere', note: 'No', from_off: 0, to_off: 9, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-08T00:00:00Z', answers: [] }] } as never]);
	const sent: unknown[] = [];
	component = mount(ChatMarginColumn, { target: host, props: { threadId: 'thread', scroller, selectedId: 'kept', onSelect: () => {}, onSendReply: (reply: unknown) => { sent.push(reply); return 'test-input'; } } });
	flushSync();

	expect(host.querySelectorAll('[data-margin-card]')).toHaveLength(2);
	// The reply carries the comment's own anchor, so it answers the same text the card quotes.
	host.querySelectorAll<HTMLElement>('[data-margin-card]')[0].querySelector<HTMLTextAreaElement>('textarea')!.value = 'Please change it';
	host.querySelectorAll<HTMLElement>('[data-margin-card]')[0].querySelector<HTMLTextAreaElement>('textarea')!.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	host.querySelectorAll<HTMLElement>('[data-margin-card]')[0].querySelector<HTMLElement>('[data-send-reply]')!.click();
	flushSync();
	expect(sent).toHaveLength(1);
	expect(sent[0]).toMatchObject({ messageId: 'm1', quote: 'runs at 2 AM', note: 'Please change it', root_user_message_id: 's1', root_cid: 'c1' });
});

it('picks a card when the reader moves into it, and shows its Reply field once it is picked', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	transcript(); thread();
	const picks: string[] = [];
	const props = $state({ threadId: 'thread', scroller, selectedId: null as string | null, onSelect: (id: string) => { picks.push(id); props.selectedId = id; }, onSendReply: vi.fn() });
	component = mount(ChatMarginColumn, { target: host, props });
	flushSync();
	// Focus picks it, the way a keyboard reader arrives.
	const card = host.querySelectorAll<HTMLElement>('[data-margin-card]')[1];
	card.querySelector<HTMLElement>('.chat-comment-thread')!.focus();
	flushSync();
	expect(picks).toEqual(['lost']);
	expect(card.querySelector('textarea')).not.toBeNull();
	expect(host.querySelectorAll<HTMLElement>('[data-margin-card]')[0].querySelector('textarea')).toBeNull();
});

it('re-places the cards when a transcript row grows with no change to its markup, as an image loading does', async () => {
	const watched: Element[] = [];
	vi.stubGlobal('ResizeObserver', class { observe(el: Element) { watched.push(el); } unobserve() {} disconnect() {} });
	transcript(); thread();
	component = mount(ChatMarginColumn, { target: host, props: { threadId: 'thread', scroller, selectedId: null, onSelect: () => {}, onSendReply: vi.fn() } });
	flushSync();
	expect(watched).toContain(scroller.querySelector('[data-message-id="m1"]'));
	// A row that arrives after the column is up (a page of older rows above it) is watched too, so it growing later moves the cards.
	const older = document.createElement('div');
	older.dataset.messageId = 'm0';
	scroller.prepend(older);
	await tick();
	expect(watched).toContain(older);
});

it('keeps the cards in place while a reply streams in, rather than rebuilding the column on every tick', () => {
	const watched: Element[] = [];
	vi.stubGlobal('ResizeObserver', class { observe(el: Element) { watched.push(el); } unobserve() {} disconnect() {} });
	transcript(); thread();
	component = mount(ChatMarginColumn, { target: host, props: { threadId: 'thread', scroller, selectedId: null, onSelect: () => {}, onSendReply: vi.fn() } });
	flushSync();
	const card = host.querySelector('[data-margin-card="kept"]');
	const observed = watched.length;
	// The typewriter's tick: a new message object, the same comments. The column's setup is not run again, so nothing is observed afresh.
	for (const pos of [1, 2, 3]) { messages.update(current => current.map(message => ({ ...message, typewriterPos: pos }))); flushSync(); }
	expect(watched).toHaveLength(observed);
	expect(host.querySelector('[data-margin-card="kept"]')).toBe(card);
	// A new comment is a change: it gets its card.
	messages.update(current => current.map(message => ({ ...message, chat_comments: [...message.chat_comments!, { ...message.chat_comments![0], id: 'added', cid: 'c3' }] })));
	flushSync();
	expect([...host.querySelectorAll<HTMLElement>('[data-margin-card]')].map(item => item.dataset.marginCard)).toEqual(['kept', 'lost', 'added']);
});

it('says why a card\'s reply is refused when its message has no place in history, and keeps the reply', async () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	transcript(); thread();
	messages.update(current => current.map(message => ({ ...message, seq: undefined })));
	const sent: unknown[] = [];
	component = mount(ChatMarginColumn, { target: host, props: { threadId: 'thread', scroller, selectedId: 'kept', onSelect: () => {}, onSendReply: (reply: unknown) => sent.push(reply) } });
	flushSync();
	const card = host.querySelectorAll<HTMLElement>('[data-margin-card]')[0];
	card.querySelector<HTMLTextAreaElement>('textarea')!.value = 'Please change it';
	card.querySelector<HTMLTextAreaElement>('textarea')!.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	card.querySelector<HTMLElement>('[data-send-reply]')!.click();
	await tick();
	flushSync();
	expect(sent).toHaveLength(0);
	const sentence = 'Couldn’t send this reply because its comment isn’t in this chat. Your reply stays here; comment on the text again.';
	// The card says why; a toast as well would repeat it over the passage the comment is on.
	expect(card.textContent).toContain(sentence);
	expect(get(toasts).map(toast => toast.message)).not.toContain(sentence);
	expect(card.querySelector<HTMLTextAreaElement>('textarea')!.value).toBe('Please change it');
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
	chatReplyDrafts.set('kept', '');
});

it('keeps an unsent reply when the canvas narrows and the same thread opens in its popover', () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	transcript(); thread();
	component = mount(ChatMarginColumn, { target: host, props: { threadId: 'thread', scroller, selectedId: 'lost', onSelect: () => {}, onSendReply: vi.fn() } });
	flushSync();
	const field = host.querySelectorAll<HTMLElement>('[data-margin-card]')[1].querySelector<HTMLTextAreaElement>('textarea')!;
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
