// An unsent chat comment, or reply in a comment thread, is kept on this device like a document's (`keptCommentDrafts`): each
// "page" here is a fresh import of the stores over the same device storage, which is what a reload, a quit and relaunch, and
// an update restart leave behind.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Page = {
	chat: typeof import('./chatCommentDrafts.svelte');
	kept: typeof import('./stores/keptCommentDrafts.svelte');
	auth: typeof import('./stores/auth');
};
let current: Page | null = null;

async function open(userId: string | null = 'person-1'): Promise<Page> {
	vi.resetModules();
	const page = {
		chat: await import('./chatCommentDrafts.svelte'),
		kept: await import('./stores/keptCommentDrafts.svelte'),
		auth: await import('./stores/auth'),
	};
	if (userId) page.auth.user.set({ id: userId, name: userId });
	current = page;
	return page;
}
/** The page is left and opened again; nothing runs on the way out. */
async function relaunch(userId: string | null = 'person-1'): Promise<Page> {
	current?.kept.keptCommentDrafts.reset();
	return open(userId);
}

const passage = { from_off: 4, to_off: 12, quote: 'a phrase', anchor_prefix: '', anchor_suffix: '' };
const other = { ...passage, from_off: 20, to_off: 28 };
const rootDraft = (page: Page, text: string) => page.chat.chatCommentDrafts.setRoot('chat-1', 'message-1', passage, text);
const stored = () => Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index)!).filter(name => name.startsWith('corsia:commentDraft:v1:'));

beforeEach(() => localStorage.clear());
afterEach(() => { current?.kept.keptCommentDrafts.reset(); current = null; vi.restoreAllMocks(); });

describe('a new comment outlives the page', () => {
	it('typed on a passage, then the page is left: the text is there when that passage is opened again', async () => {
		let page = await open();
		rootDraft(page, 'A comment I did not send');
		page = await relaunch();
		expect(page.chat.chatCommentDrafts.root('chat-1', 'message-1', passage)?.text).toBe('A comment I did not send');
	});

	it('is the passage’s own: another passage, message or chat does not show it', async () => {
		let page = await open();
		rootDraft(page, 'Only for this passage');
		page = await relaunch();
		const { chatCommentDrafts } = page.chat;
		expect(chatCommentDrafts.root('chat-1', 'message-1', other)).toBeUndefined();
		expect(chatCommentDrafts.root('chat-1', 'message-2', passage)).toBeUndefined();
		expect(chatCommentDrafts.root('chat-2', 'message-1', passage)).toBeUndefined();
	});

	it('is kept as a document’s is: one entry under the signed-in person, with no failure or posting state', async () => {
		let page = await open();
		rootDraft(page, 'Kept text');
		expect(stored()).toHaveLength(1);
		expect(stored()[0]).toContain('corsia:commentDraft:v1:person-1:chat:');
		const refused = Promise.reject(new Error('no'));
		refused.catch(() => {});
		await page.chat.chatCommentDrafts.postRoot('chat-1', 'message-1', passage, 'Kept text', () => refused);
		expect(page.chat.chatCommentDrafts.root('chat-1', 'message-1', passage)?.error).toBeTruthy();
		page = await relaunch();
		const draft = page.chat.chatCommentDrafts.root('chat-1', 'message-1', passage);
		expect(draft?.text).toBe('Kept text');
		expect(draft?.error).toBeUndefined();
		expect(draft?.posting).toBeUndefined();
	});

	it('Cancel and a stored post both take it off the device', async () => {
		let page = await open();
		rootDraft(page, 'Cancelled');
		page.chat.chatCommentDrafts.dropRoot('chat-1', 'message-1', passage);
		expect(stored()).toEqual([]);
		rootDraft(page, 'Stored');
		expect(await page.chat.chatCommentDrafts.postRoot('chat-1', 'message-1', passage, 'Stored', () => Promise.resolve())).toBe(true);
		expect(stored()).toEqual([]);
		page = await relaunch();
		expect(page.chat.chatCommentDrafts.root('chat-1', 'message-1', passage)).toBeUndefined();
	});
});

describe('a reply in a comment thread outlives the page', () => {
	it('typed in a thread, then the page is left: the reply is in that thread’s field', async () => {
		let page = await open();
		page.chat.chatCommentDrafts.set('comment-uuid-1', 'A reply I did not send');
		page = await relaunch();
		expect(page.chat.chatCommentDrafts.get('comment-uuid-1')).toBe('A reply I did not send');
		expect(page.chat.chatCommentDrafts.get('comment-uuid-2')).toBe('');
	});

	it('emptying the field clears it from the device, and a stored reply is not kept', async () => {
		let page = await open();
		page.chat.chatCommentDrafts.set('comment-uuid-1', 'Gone soon');
		page.chat.chatCommentDrafts.set('comment-uuid-1', '');
		expect(stored()).toEqual([]);
		page.chat.chatCommentDrafts.set('comment-uuid-2', 'Sent');
		expect(await page.chat.chatCommentDrafts.postReply('comment-uuid-2', 'Sent', () => Promise.resolve())).toBe(true);
		page = await relaunch();
		expect(page.chat.chatCommentDrafts.get('comment-uuid-1')).toBe('');
		expect(page.chat.chatCommentDrafts.get('comment-uuid-2')).toBe('');
	});
});

describe('Sign out', () => {
	it('clears the person’s chat drafts from the page and the device; another person’s stay', async () => {
		let page = await open('person-2');
		rootDraft(page, 'Theirs');
		page = await relaunch('person-1');
		rootDraft(page, 'Mine');
		page.chat.chatCommentDrafts.set('comment-uuid-1', 'My reply');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) })));
		await page.auth.logout();
		expect(page.chat.chatCommentDrafts.root('chat-1', 'message-1', passage)).toBeUndefined();
		expect(page.chat.chatCommentDrafts.get('comment-uuid-1')).toBe('');
		page = await relaunch('person-1');
		expect(page.chat.chatCommentDrafts.root('chat-1', 'message-1', passage)).toBeUndefined();
		expect(page.chat.chatCommentDrafts.get('comment-uuid-1')).toBe('');
		page = await relaunch('person-2');
		expect(page.chat.chatCommentDrafts.root('chat-1', 'message-1', passage)?.text).toBe('Theirs');
	});
});

describe('the draft’s one client id', () => {
	it.each([
		['a new comment', (page: Page, text: string, send: (id: string) => Promise<void>) => page.chat.chatCommentDrafts.postRoot('chat-1', 'message-1', passage, text, send), (page: Page, text: string) => rootDraft(page, text)],
		['a reply', (page: Page, text: string, send: (id: string) => Promise<void>) => page.chat.chatCommentDrafts.postReply('comment-uuid-1', text, send), (page: Page, text: string) => page.chat.chatCommentDrafts.set('comment-uuid-1', text)],
	])('%s posts under the id its kept draft holds, and a press again after a restart posts under the same id', async (_name, press, type) => {
		let page = await open();
		type(page, 'Same words');
		const sent: string[] = [];
		const lost = (id: string) => { sent.push(id); return Promise.reject(new Error('answer lost')); };
		expect(await press(page, 'Same words', lost)).toBe(false);
		expect(sent[0]).toBeTruthy();
		page = await relaunch();
		expect(await press(page, 'Same words', (id) => { sent.push(id); return Promise.resolve(); })).toBe(true);
		expect(sent).toEqual([sent[0], sent[0]]);
	});

	it.each([
		['a new comment', (page: Page, text: string, send: (id: string) => Promise<void>) => page.chat.chatCommentDrafts.postRoot('chat-1', 'message-1', passage, text, send), (page: Page, text: string) => rootDraft(page, text)],
		['a reply', (page: Page, text: string, send: (id: string) => Promise<void>) => page.chat.chatCommentDrafts.postReply('comment-uuid-1', text, send), (page: Page, text: string) => page.chat.chatCommentDrafts.set('comment-uuid-1', text)],
	])('%s edited after a press posts as its own comment, under a fresh id', async (_name, press, type) => {
		const page = await open();
		type(page, 'First words');
		const sent: string[] = [];
		await press(page, 'First words', (id) => { sent.push(id); return Promise.reject(new Error('answer lost')); });
		type(page, 'First words, and more');
		await press(page, 'First words, and more', (id) => { sent.push(id); return Promise.resolve(); });
		expect(sent).toHaveLength(2);
		expect(sent[1]).not.toBe(sent[0]);
	});

	it('Cancel, then a new comment on the same passage, posts under a new id', async () => {
		const page = await open();
		rootDraft(page, 'One');
		const sent: string[] = [];
		await page.chat.chatCommentDrafts.postRoot('chat-1', 'message-1', passage, 'One', (id) => { sent.push(id); return Promise.reject(new Error('no')); });
		page.chat.chatCommentDrafts.dropRoot('chat-1', 'message-1', passage);
		rootDraft(page, 'One');
		await page.chat.chatCommentDrafts.postRoot('chat-1', 'message-1', passage, 'One', (id) => { sent.push(id); return Promise.resolve(); });
		expect(sent[1]).not.toBe(sent[0]);
	});
});
