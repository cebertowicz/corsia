import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, expect, it, vi } from 'vitest';
import { activeThreadId, messages } from '$lib/stores/threads';
import { chatCommentDrafts } from '$lib/chatCommentDrafts.svelte';
import { resetReplacementsForTests } from '$lib/messageReplacements';
import { handleMessage } from '$lib/ws/handlers';
import { postChatComment } from './composerSend';
import Host from './ChatCommentSelectionPickHost.test.svelte';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
let component: ReturnType<typeof mount> | undefined;
const anchor = { from_off: 0, to_off: 4, quote: 'text', anchor_prefix: '', anchor_suffix: ' more' };
afterEach(() => {
	if (component) unmount(component);
	component = undefined;
	chatCommentDrafts.dropRoot('thread', 'replacement-old', anchor);
	messages.set([]); activeThreadId.set(null); resetReplacementsForTests();
	document.body.replaceChildren(); vi.unstubAllGlobals();
	Reflect.deleteProperty(Range.prototype, 'getClientRects');
});

it('keeps a typed comment accessible when its row is replaced and its post is refused', async () => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {}); vi.stubGlobal('innerWidth', 1440);
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'comment_quote_gone' }), { status: 400 })));
	Object.defineProperty(Range.prototype, 'getClientRects', { value: () => [{ top: 120, bottom: 140, left: 10, right: 60 }], configurable: true });
	document.body.innerHTML = '<div class="messages"><div class="message assistant" data-message-id="replacement-old"><div class="long-message-body">text more</div></div></div><div id="mount"></div>';
	activeThreadId.set('thread');
	messages.set([{ id: 'replacement-old', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [] }] as never);
	component = mount(Host, { target: document.querySelector('#mount')!, props: {
		onSendReply: reply => postChatComment(reply, 'thread', false, true),
	} });
	await tick();
	const range = document.createRange();
	range.setStart(document.querySelector('.long-message-body')!.firstChild!, 0);
	range.setEnd(document.querySelector('.long-message-body')!.firstChild!, 4);
	document.getSelection()!.removeAllRanges(); document.getSelection()!.addRange(range);
	document.dispatchEvent(new Event('selectionchange')); flushSync();
	document.querySelector<HTMLButtonElement>('.sel-comment')!.click(); await tick(); flushSync();
	const field = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!;
	field.value = 'Keep this unsent thought'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
	handleMessage({ type: 'messages_replaced', thread_id: 'thread', removed_message_ids: ['replacement-old'], message: {
		id: 'replacement-new', seq: 2, role: 'assistant', content: 'new text', created_at: '2026-11-14T00:00:00', replaced_message_ids: ['replacement-old'],
	} }, () => {});
	document.querySelector('[data-message-id="replacement-old"]')!.remove(); await tick(); flushSync();
	expect(get(messages).map(row => row.id)).toEqual(['replacement-new']);
	expect(field.isConnected).toBe(true); expect(field.value).toBe('Keep this unsent thought');
	document.querySelector<HTMLButtonElement>('[role="dialog"] button.submit')!.click();
	await vi.waitFor(() => {
		flushSync();
		expect(document.querySelector('[role="alert"]')?.textContent).toBe('The text this comment quotes isn’t in the chat, so this comment couldn’t be sent. Your text stays here to copy.');
	});
	expect(field.isConnected).toBe(true); expect(field.value).toBe('Keep this unsent thought');
	expect(field.readOnly).toBe(false);
	expect(document.querySelector('[role="dialog"] button.submit')).toBeNull();
});
