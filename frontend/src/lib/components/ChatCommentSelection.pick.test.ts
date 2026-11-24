import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatCommentSelectionPickHost from './ChatCommentSelectionPickHost.test.svelte';
import { messages } from '$lib/stores/threads';
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';

// Where the column is up, the transcript's highlight is the control that picks its card, so the picked id
// has to reach the caller that shares it with the column. This walks the real surface through the same
// binding `ChatPanel` uses: a painted highlight, the click on it, then the first Escape.

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function setupSurface(): void {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.stubGlobal('innerWidth', 1440);
	host = document.createElement('div');
	host.innerHTML = '<div class="messages"><div class="message assistant" data-message-id="m1"><div class="long-message-body">text more</div></div></div><div id="mount"></div>';
	document.body.append(host);
}

const picked = () => host.querySelector<HTMLElement>('[data-picked]')?.dataset.picked ?? null;

afterEach(() => {
	if (component) unmount(component as never);
	component = null;
	messages.set([]);
	host?.remove();
	vi.unstubAllGlobals();
});

it('gives the caller the comment its highlight picked, and the first Escape drops the pick', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	expect(picked()).toBe('one');
	// Nothing else opened: the thread is already on screen beside its text.
	expect(host.querySelector('[role="dialog"]')).toBeNull();
	document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
	flushSync();
	await tick();
	expect(picked()).toBe('');
});

it('releases a picked margin card on plain transcript text or empty transcript, and preserves the draft', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	chatCommentDrafts.set('one', 'unsent reply');
	const mark = host.querySelector<HTMLElement>('[data-comment-ids="one"]')!;
	for (const target of [host.querySelector<HTMLElement>('.long-message-body')!, host.querySelector<HTMLElement>('.messages')!]) {
		mark.click(); flushSync();
		expect(picked()).toBe('one');
		target.click(); flushSync();
		expect(picked()).toBe('');
		expect(chatCommentDrafts.get('one')).toBe('unsent reply');
	}
	mark.click(); flushSync();
	expect(picked()).toBe('one');
	expect(chatCommentDrafts.get('one')).toBe('unsent reply');
	// The margin lives inside the transcript scroller; clicking a card is not a click off it.
	host.querySelector('.messages')!.insertAdjacentHTML('beforeend', '<div class="chat-margin"><div class="chat-margin-card">Card text</div></div>');
	const card = host.querySelector<HTMLElement>('.chat-margin-card')!;
	card.click(); flushSync();
	expect(picked()).toBe('one');
	// A card can move on pointerdown, leaving the click on the scroller instead of the pressed field.
	card.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
	host.querySelector('.messages')!.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
	flushSync();
	expect(picked()).toBe('one');
	// The next independent press on the transcript still releases it.
	host.querySelector('.messages')!.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
	host.querySelector('.messages')!.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
	flushSync();
	expect(picked()).toBe('');
	chatCommentDrafts.set('one', '');
});

it('gives focus in the released card back to its highlight on Escape, as a document margin gives it back to the text, keeping the unsent reply', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	// The column's card for the comment, holding its Reply field.
	host.insertAdjacentHTML('beforeend', '<div class="chat-margin"><div class="chat-margin-card"><div data-comment-uuid="one" tabindex="-1"><textarea></textarea></div></div></div>');
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	const mark = host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!;
	mark.click();
	flushSync();
	await tick();
	expect(picked()).toBe('one');
	const field = host.querySelector<HTMLTextAreaElement>('.chat-margin textarea')!;
	field.focus();
	field.value = 'A reply not sent yet';
	document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
	flushSync();
	await tick();
	expect(picked()).toBe('');
	expect(document.activeElement).toBe(mark);
	expect(field.value).toBe('A reply not sent yet');
});

it('leaves focus outside the column where it is when Escape releases the picked card', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	host.insertAdjacentHTML('beforeend', '<div class="chat-margin"><div class="chat-margin-card"><div data-comment-uuid="one" tabindex="-1"><textarea></textarea></div></div></div><textarea id="composer"></textarea>');
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	const composer = host.querySelector<HTMLTextAreaElement>('#composer')!;
	composer.focus();
	composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
	flushSync();
	await tick();
	expect(picked()).toBe('');
	expect(document.activeElement).toBe(composer);
});

it('reopens a thread with its reply after switching while its close is pending', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	host.querySelector<HTMLButtonElement>('[data-toggle-margin]')!.click();
	flushSync();
	await vi.waitFor(() => expect(host.querySelector('[data-comment-ids="one"]')).not.toBeNull());
	const mark = host.querySelector<HTMLElement>('[data-comment-ids="one"]')!;
	mark.click(); flushSync(); await tick(); flushSync();
	const field = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!;
	field.value = 'unsent reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
	document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
	host.querySelector<HTMLButtonElement>('[data-toggle-thread]')!.click(); flushSync(); await tick();
	host.querySelector<HTMLButtonElement>('[data-toggle-thread]')!.click(); flushSync(); await tick();
	messages.update(items => [...items]);
	await vi.waitFor(() => expect(host.querySelector('[data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('[data-comment-ids="one"]')!.click(); flushSync(); await tick(); flushSync();
	await vi.waitFor(() => expect(document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')?.value).toBe('unsent reply'));
	chatCommentDrafts.set('one', '');
});

it('hands a thread open in its popover to its card when the column comes up, keeping the unsent reply', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	// Narrow: the highlight opens the thread in its popover.
	host.querySelector<HTMLButtonElement>('[data-toggle-margin]')!.click();
	flushSync();
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	flushSync();
	const field = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!;
	field.value = 'keep this draft';
	field.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();

	// Widen: the card takes the thread, so it is open once, and the reply typed in the popover is the card's.
	host.querySelector<HTMLButtonElement>('[data-toggle-margin]')!.click();
	flushSync();
	await tick();
	flushSync();
	expect(document.querySelector('[role="dialog"]')).toBeNull();
	expect(picked()).toBe('one');
	expect(chatCommentDrafts.get('one')).toBe('keep this draft');
	chatCommentDrafts.set('one', '');
});

it('writes a new comment in the column, as a card level with its text, while the column is up', async () => {
	setupSurface();
	Object.defineProperty(Range.prototype, 'getClientRects', { value: () => [{ top: 120, bottom: 140, left: 10, right: 60 }], configurable: true });
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [] }] as never);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await tick();
	const range = document.createRange();
	range.setStart(host.querySelector('.long-message-body')!.firstChild!, 0);
	range.setEnd(host.querySelector('.long-message-body')!.firstChild!, 4);
	document.getSelection()!.removeAllRanges();
	document.getSelection()!.addRange(range);
	document.dispatchEvent(new Event('selectionchange'));
	flushSync();
	host.querySelector<HTMLButtonElement>('.sel-comment')!.click();
	flushSync();
	await tick();
	flushSync();

	// The column draws the composer; nothing opens over the text.
	const writing = host.querySelector<HTMLElement>('[data-margin-composer="composer"]')!;
	expect(writing.querySelector('[role="dialog"] textarea')).not.toBeNull();
	expect(document.querySelector('[data-popover]')).toBeNull();
	// Cancel closes it at once: there is no popover to wait out.
	writing.querySelector<HTMLButtonElement>('button.cancel')!.click();
	flushSync();
	await tick();
	flushSync();
	expect(host.querySelector<HTMLElement>('[data-margin-composer]')!.dataset.marginComposer).toBe('');
	Reflect.deleteProperty(Range.prototype, 'getClientRects');
});

it('returns focus to the pressed highlight when a thread opened from the list closes', async () => {
	setupSurface();
	vi.stubGlobal('innerWidth', 390);
	const comment = (id: string) => ({ id, cid: `c-${id}`, user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] });
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [comment('one'), comment('two')] }] as never);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	// Narrow: the host starts with the column up, so toggle it away and the press opens the list in a sheet.
	host.querySelector<HTMLButtonElement>('[data-toggle-margin]')!.click();
	flushSync();
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids]')).not.toBeNull());
	// Press the innermost highlight: every comment on the text is on it or on a span around it.
	const highlight = host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="two"]')!;
	highlight.click();
	flushSync();
	await tick();
	flushSync();
	// Two comments on the pressed text open as a list; a row's Reply opens that thread and the button leaves the page.
	const reply = document.querySelector<HTMLButtonElement>('[role="dialog"] [data-reply-comment="two"]')!;
	reply.click();
	flushSync();
	await tick();
	flushSync();
	expect(reply.isConnected).toBe(false);
	document.querySelector<HTMLButtonElement>('[role="dialog"] button[aria-label="Close"]')!.click();
	await vi.waitFor(() => expect(document.activeElement).toBe(highlight));
});

it('lets the picked card go on a press that began on the transcript, never on one that began in the card and was released beside it', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	// The column sits inside the transcript, as it does in the app.
	host.querySelector('.messages')!.insertAdjacentHTML('beforeend', '<div class="chat-margin"><div class="chat-margin-card"><div data-comment-uuid="one" tabindex="-1"><textarea></textarea></div></div></div>');
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	expect(picked()).toBe('one');
	const transcript = host.querySelector<HTMLElement>('.messages')!;
	const press = (down: Element, clicked: Element) => {
		down.dispatchEvent(new Event('pointerdown', { bubbles: true, composed: true }));
		clicked.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
		flushSync();
	};
	// The card moved under a still pointer between press and release, so the click lands on what both share: the transcript.
	press(host.querySelector('.chat-margin textarea')!, transcript);
	await tick();
	expect(picked()).toBe('one');
	// A press that began on the transcript, away from any highlight or card, lets it go.
	press(host.querySelector('.long-message-body')!, host.querySelector('.long-message-body')!);
	await tick();
	expect(picked()).toBe('');
});

it('lets the picked card go on a drag that began on the transcript and was released on the card: where the press went down decides', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	host.querySelector('.messages')!.insertAdjacentHTML('beforeend', '<div class="chat-margin"><div class="chat-margin-card"><div data-comment-uuid="one" tabindex="-1"><textarea></textarea></div></div></div>');
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	expect(picked()).toBe('one');
	// Down on the transcript's text, up on the card's field: the click lands on what both share, the transcript.
	host.querySelector('.long-message-body')!.dispatchEvent(new Event('pointerdown', { bubbles: true, composed: true }));
	host.querySelector('.chat-margin textarea')!.dispatchEvent(new Event('pointerup', { bubbles: true, composed: true }));
	host.querySelector('.messages')!.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
	flushSync();
	await tick();
	expect(picked()).toBe('');
});
