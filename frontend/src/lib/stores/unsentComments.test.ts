// A comment the person pressed Comment on goes on its own when they open its document, when the connection comes up and
// when they return to the app: once per trigger, one at a time, and never once it is refused or edited. Each "page" is a
// fresh import of the stores over the same device storage, as a reload leaves it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import type { DraftSend } from './keptCommentDraftsStorage';

const wire = vi.hoisted(() => ({
	posts: [] as { artifactId: string; body: Record<string, unknown> }[],
	answer: [] as ((body: Record<string, unknown>) => Promise<unknown>)[],
	stored: [] as Record<string, unknown>[],
	onScreen: false,
}));
vi.mock('$lib/api/docs', async (original) => ({
	...await original<typeof import('$lib/api/docs')>(),
	postComment: async (artifactId: string, body: Record<string, unknown>) => {
		wire.posts.push({ artifactId, body });
		const next = wire.answer.shift();
		return next ? next(body) : row(body);
	},
	fetchComments: async () => ({ comments: wire.stored, feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } }),
}));
vi.mock('$lib/elementOnScreen', () => ({ elementOnScreen: () => wire.onScreen }));

const row = (body: Record<string, unknown>) => ({
	id: body.client_id, body: String(body.body).trim(), tagged_agent_ids: body.tagged_agent_ids ?? [], anchor_text: body.anchor_text,
	from_off: body.from_off, to_off: body.to_off, author: 'you', author_kind: 'user', created_at: '2026-11-26T00:00:00Z', replies: [],
});

type Page = {
	queue: typeof import('./unsentComments.svelte'); drafts: typeof import('./keptCommentDrafts.svelte'); auth: typeof import('./auth');
	connection: typeof import('./connection'); toast: typeof import('./toast'); docs: typeof import('$lib/api/docs');
	tags: typeof import('$lib/components/comments/mentionSelection'); draft: typeof import('./commentDraft');
};
let current: Page | null = null;

async function open(): Promise<Page> {
	current?.drafts.keptCommentDrafts.reset();
	current?.queue.unsentComments.reset();
	vi.resetModules();
	const page: Page = {
		queue: await import('./unsentComments.svelte'), drafts: await import('./keptCommentDrafts.svelte'), auth: await import('./auth'),
		connection: await import('./connection'), toast: await import('./toast'), docs: await import('$lib/api/docs'),
		tags: await import('$lib/components/comments/mentionSelection'), draft: await import('./commentDraft'),
	};
	page.auth.user.set({ id: 'person-1', name: 'person-1' });
	return current = page;
}
const KEY = (page: Page, path = 'plans/notes.md') => page.drafts.documentDraftKey({ projectId: 'project-1', path });
const PLACE = { from: 60, to: 66, quote: 'Friday', prefix: 'll a side.\n\nThe launch moves to ', suffix: '.\nNotes follow on the next line ' };
const SEND = { artifactId: 'artifact-1', threadId: 'thread-1' };

/** The draft typed on its passage, then Comment pressed on it, as its box does. */
function pressed(page: Page, { body = 'Move it to Monday please, the team is out', key = KEY(page), place = PLACE as typeof PLACE | null, send = SEND as DraftSend } = {}) {
	const tags = page.tags.createMentionSelection(body), drafts = page.drafts.keptCommentDrafts;
	drafts.keep(key, body, tags, place);
	return { key, tags, press: drafts.claim(key, body, tags, undefined, send) };
}
async function connect(page: Page): Promise<void> {
	page.connection.connectionState.set('disconnected');
	page.connection.connectionState.set('connected');
	await page.queue.unsentComments.settled();
}
async function returnToApp(page: Page): Promise<void> {
	document.dispatchEvent(new Event('visibilitychange'));
	await page.queue.unsentComments.settled();
}
const refusal = (page: Page, status: number, kind: string | null, detail: string) => async () => {
	throw new page.docs.RequestRefused(`${status} ${JSON.stringify({ detail, kind })}`, kind);
};
const offline = async () => { throw new TypeError('Failed to fetch'); };
const PASSAGE_GONE = 'The text this comment was on changed before it sent, so it isn’t placed. Your draft is kept.';

beforeEach(() => { localStorage.clear(); wire.posts = []; wire.answer = []; wire.stored = []; wire.onScreen = false; });
afterEach(() => {
	for (const toast of current ? get(current.toast.toasts) : []) current?.toast.toasts.dismiss(toast.id);
	current?.drafts.keptCommentDrafts.reset(); current?.queue.unsentComments.reset(); current = null; localStorage.clear();
});

describe('a pressed comment goes on its own', () => {
	it('after a reload, on the first connection, on its passage by its sides, under the id its press used', async () => {
		const { key, press } = pressed(await open());
		const page = await open();
		await connect(page);
		expect(wire.posts).toHaveLength(1);
		expect(wire.posts[0]).toMatchObject({ artifactId: 'artifact-1', body: {
			client_id: press.clientId, body: 'Move it to Monday please, the team is out', anchor_text: 'Friday', from_off: 60, to_off: 66,
			anchor_prefix: PLACE.prefix, anchor_suffix: PLACE.suffix, thread_id: 'thread-1',
		} });
		// No capture is held for it on this page: the server places it by its sides.
		expect(wire.posts[0].body).not.toHaveProperty('live_capture');
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('when its document is opened, and its open box closes once it posts', async () => {
		const page = await open(), { key } = pressed(page);
		page.draft.commentDraft.set({ from: 60, to: 66, text: 'Friday', top: 0, key });
		expect(page.queue.unsentComments.sending(key)).toBe(true);
		page.queue.unsentComments.open(key);
		await page.queue.unsentComments.settled();
		expect(wire.posts).toHaveLength(1);
		expect(get(page.draft.commentDraft)).toBeNull();
	});

	it('when the person returns to the app', async () => {
		const page = await open();
		pressed(page);
		await returnToApp(page);
		expect(wire.posts).toHaveLength(1);
	});
});

describe('only what was pressed goes, and only where it can be told apart', () => {
	it('a draft typed and never pressed is never sent, whatever the trigger', async () => {
		const page = await open(), key = KEY(page);
		page.drafts.keptCommentDrafts.keep(key, 'Typed only', page.tags.createMentionSelection('Typed only'), PLACE);
		await connect(page);
		await returnToApp(page);
		page.queue.unsentComments.open(key);
		await page.queue.unsentComments.settled();
		expect(wire.posts).toHaveLength(0);
		expect(page.queue.unsentComments.sending(key)).toBe(false);
	});

	it('an edit after the press makes it typed again: the next trigger sends nothing', async () => {
		const page = await open(), { key, tags } = pressed(page);
		page.drafts.keptCommentDrafts.keep(key, 'Move it to Tuesday', tags);
		await connect(page);
		expect(wire.posts).toHaveLength(0);
		expect(page.queue.unsentComments.sending(key)).toBe(false);
	});

	it('a draft whose passage could not be told apart when kept is not queued and sends no sides', async () => {
		const page = await open(), { key } = pressed(page, { place: null });
		await connect(page);
		page.queue.unsentComments.open(key);
		await page.queue.unsentComments.settled();
		expect(wire.posts).toHaveLength(0);
		expect(page.queue.unsentComments.sending(key)).toBe(false);
	});

	it('an edit while its post is out: the pressed words are stored, the edit stays typed, and nothing more is sent', async () => {
		const page = await open(), { key, tags } = pressed(page);
		let release!: () => void;
		wire.answer.push((body) => new Promise((done) => { release = () => done(row(body)); }));
		page.queue.unsentComments.open(key);
		await vi.waitFor(() => expect(wire.posts).toHaveLength(1));
		page.drafts.keptCommentDrafts.keep(key, 'Move it to Tuesday', tags);
		release();
		await page.queue.unsentComments.settled();
		expect(page.drafts.keptCommentDrafts.get(key)).toMatchObject({ body: 'Move it to Tuesday', place: PLACE });
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
		await connect(page);
		expect(wire.posts).toHaveLength(1);
	});
});

describe('what the server answers', () => {
	it('a failure that passes says nothing, keeps the draft sending, and the next trigger tries again under the same id', async () => {
		const page = await open(), { key, press } = pressed(page);
		wire.answer.push(offline);
		await connect(page);
		expect(page.drafts.keptCommentDrafts.get(key)).toMatchObject({ clientId: press.clientId, send: SEND });
		expect(page.drafts.keptCommentDrafts.get(key)?.error).toBeUndefined();
		expect(get(page.toast.toasts)).toHaveLength(0);
		expect(page.queue.unsentComments.sending(key)).toBe(true);
		await connect(page);
		expect(wire.posts.map((post) => post.body.client_id)).toEqual([press.clientId, press.clientId]);
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('a refusal stops it for good: the words stay with the server’s reason, and a notice names the document once', async () => {
		const page = await open(), { key } = pressed(page);
		wire.answer.push(refusal(page, 409, 'passage_gone', PASSAGE_GONE));
		await connect(page);
		expect(page.drafts.keptCommentDrafts.get(key)).toMatchObject({ body: 'Move it to Monday please, the team is out', error: PASSAGE_GONE, place: PLACE });
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
		expect(get(page.toast.toasts).map((toast) => [toast.message, toast.type])).toEqual([
			[`Your comment on notes.md, “Move it to Monday please, the…”: ${PASSAGE_GONE}`, 'error'],
		]);
		await connect(page);
		await returnToApp(page);
		expect(wire.posts).toHaveLength(1);
		expect(get(page.toast.toasts)).toHaveLength(1);
	});

	it('a refusal while its box is on screen says why there only', async () => {
		const page = await open(), { key } = pressed(page);
		page.draft.commentDraft.set({ from: 60, to: 66, text: 'Friday', top: 0, key });
		const box = document.createElement('div');
		page.drafts.keptCommentDrafts.shown(key, box);
		wire.onScreen = true;
		wire.answer.push(refusal(page, 409, 'passage_gone', PASSAGE_GONE));
		page.queue.unsentComments.open(key);
		await page.queue.unsentComments.settled();
		expect(get(page.draft.commentDraft)?.error).toBe(PASSAGE_GONE);
		expect(get(page.toast.toasts)).toHaveLength(0);
	});

	it.each([
		['the document is gone', 404, null],
		['a tag is not allowed', 400, 'tag_unavailable'],
		['the document no longer takes comments', 409, 'not_commentable'],
	])('%s: refused, not tried again', async (_name, status, kind) => {
		const page = await open(), { key } = pressed(page);
		wire.answer.push(refusal(page, status, kind, 'No.'));
		await connect(page);
		await connect(page);
		expect(wire.posts).toHaveLength(1);
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
		expect(page.drafts.keptCommentDrafts.get(key)?.error).toBeTruthy();
	});

	it.each([
		['a server error', 500, null],
		['too many requests', 429, null],
		['an id still settling', 409, 'identity_settling'],
		['a 409 with no reason', 409, null],
	])('%s: tried again on the next trigger', async (_name, status, kind) => {
		const page = await open(), { key } = pressed(page);
		wire.answer.push(refusal(page, status, kind, 'later'));
		await connect(page);
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toEqual(SEND);
		await connect(page);
		expect(wire.posts).toHaveLength(2);
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('a signed-out answer waits for sign-in: nothing goes while signed out, and it goes on the connection after', async () => {
		const page = await open(), { key } = pressed(page);
		wire.answer.push(refusal(page, 401, 'sign_in_required', 'Sign in.'));
		await connect(page);
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toEqual(SEND);
		page.auth.user.set(null);
		await returnToApp(page);
		expect(wire.posts).toHaveLength(1);
		page.auth.user.set({ id: 'person-1', name: 'person-1' });
		await connect(page);
		expect(wire.posts).toHaveLength(2);
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('an id that is another comment’s goes once more under a new id', async () => {
		const page = await open(), { key, press } = pressed(page);
		wire.answer.push(refusal(page, 409, 'client_id_clash', 'client_id collides'));
		await connect(page);
		expect(wire.posts).toHaveLength(2);
		expect(wire.posts[1].body.client_id).not.toBe(press.clientId);
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('an earlier version stored under its id shows, and the changes stay typed with a fresh id', async () => {
		const page = await open(), { key } = pressed(page);
		wire.answer.push(async (body) => row({ ...body, body: 'Move it to Sunday' }));
		await connect(page);
		const draft = page.drafts.keptCommentDrafts.get(key);
		expect(draft).toMatchObject({ body: 'Move it to Monday please, the team is out', error: 'Your earlier version was posted. Your changes are kept here.' });
		expect(draft?.send).toBeUndefined();
		expect(draft?.clientId).toBeUndefined();
	});
});

describe('how often it tries', () => {
	it('one at a time across the app, each draft once per trigger', async () => {
		const page = await open();
		pressed(page);
		pressed(page, { key: KEY(page, 'plans/other.md'), send: { artifactId: 'artifact-2', threadId: null } });
		let inFlight = 0, most = 0;
		const held = async (body: Record<string, unknown>) => {
			most = Math.max(most, ++inFlight);
			await new Promise((done) => setTimeout(done, 5));
			inFlight -= 1;
			return row(body);
		};
		wire.answer.push(held, held);
		page.connection.connectionState.set('disconnected');
		page.connection.connectionState.set('connected');
		page.queue.unsentComments.open(KEY(page));
		page.queue.unsentComments.open(KEY(page, 'plans/other.md'));
		await page.queue.unsentComments.settled();
		expect(most).toBe(1);
		expect(wire.posts.map((post) => post.artifactId)).toEqual(['artifact-1', 'artifact-2']);
	});

	it('a return to the app within 10 s of a draft’s last try does not try it again', async () => {
		const page = await open(), { key } = pressed(page);
		const now = vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
		wire.answer.push(offline, offline);
		await returnToApp(page);
		now.mockReturnValue(1_009_000);
		await returnToApp(page);
		expect(wire.posts).toHaveLength(1);
		now.mockReturnValue(1_011_000);
		await returnToApp(page);
		expect(wire.posts).toHaveLength(2);
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toEqual(SEND);
		now.mockRestore();
	});
});

describe('a pressed comment on an image goes on its own', () => {
	const REGION = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
	const IMAGE_SEND: DraftSend = { artifactId: 'image-1', threadId: 'thread-1', diagramSource: 'graph TD; A-->B', name: 'flow.png' };
	const MARKDOWN = { from: 40, to: 57, quote: '![flow](flow.png)', prefix: 'See the chart below.\n\n', suffix: '\n\nNotes follow.' };
	/** Drawn on its area and typed, then Comment pressed on it, as its box does. */
	function pressedOnImage(page: Page, { key = page.drafts.imageDraftKey('image-1'), place = undefined as typeof MARKDOWN | null | undefined, send = IMAGE_SEND } = {}) {
		const body = 'Label this arrow', tags = page.tags.createMentionSelection(body), drafts = page.drafts.keptCommentDrafts;
		drafts.keep(key, body, tags, place, REGION);
		return { key, press: drafts.claim(key, body, tags, undefined, send) };
	}

	it('an image artifact’s, after a reload, on its area and the diagram it was drawn on, under its press’s id', async () => {
		const { key, press } = pressedOnImage(await open());
		const page = await open();
		await connect(page);
		expect(wire.posts).toHaveLength(1);
		expect(wire.posts[0]).toMatchObject({ artifactId: 'image-1', body: {
			client_id: press.clientId, body: 'Label this arrow', region: REGION, diagram_source: 'graph TD; A-->B', thread_id: 'thread-1',
		} });
		expect(wire.posts[0].body).not.toHaveProperty('anchor_text');
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('an image inside a document’s, on its area and on the image’s markdown by its sides', async () => {
		const page = await open(), key = page.drafts.imageDraftKey('doc-1', 'flow.png');
		pressedOnImage(page, { key, place: MARKDOWN, send: { artifactId: 'doc-1', threadId: null, name: 'notes.md' } });
		page.queue.unsentComments.open(key);
		await page.queue.unsentComments.settled();
		expect(wire.posts).toHaveLength(1);
		expect(wire.posts[0]).toMatchObject({ artifactId: 'doc-1', body: {
			region: REGION, anchor_text: MARKDOWN.quote, from_off: 40, to_off: 57, anchor_prefix: MARKDOWN.prefix, anchor_suffix: MARKDOWN.suffix,
		} });
	});

	it('an image inside a document whose markdown could not be told apart when kept is not sent; one beside it that could be goes', async () => {
		const page = await open();
		const lost = page.drafts.imageDraftKey('doc-1', 'flow.png'), placed = page.drafts.imageDraftKey('doc-1', 'other.png');
		pressedOnImage(page, { key: lost, place: null, send: { artifactId: 'doc-1', threadId: null } });
		pressedOnImage(page, { key: placed, place: MARKDOWN, send: { artifactId: 'doc-1', threadId: null } });
		await connect(page);
		page.queue.unsentComments.open(lost);
		await page.queue.unsentComments.settled();
		expect(wire.posts).toHaveLength(1);
		expect(wire.posts[0].body).toMatchObject({ anchor_text: MARKDOWN.quote });
		expect(page.queue.unsentComments.sending(lost)).toBe(false);
		expect(page.drafts.keptCommentDrafts.get(placed)).toBeUndefined();
	});

	it('the image is gone (404): refused, kept with its words and area, and a notice names the image', async () => {
		const page = await open(), { key } = pressedOnImage(page);
		wire.answer.push(refusal(page, 404, null, 'Not found'));
		await connect(page);
		await connect(page);
		expect(wire.posts).toHaveLength(1);
		expect(page.drafts.keptCommentDrafts.get(key)).toMatchObject({ body: 'Label this arrow', region: REGION });
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
		expect(get(page.toast.toasts).map((toast) => toast.message)).toEqual([expect.stringMatching(/^Your comment on flow\.png, “Label this arrow”: /)]);
	});
});

describe('a pressed reply on a document’s or an image’s thread goes on its own', () => {
	const REPLY_SEND: DraftSend = { artifactId: 'artifact-1', threadId: 'thread-1' };
	const replyKey = (page: Page, scope = KEY(page)) => page.drafts.replyDraftKey(scope, 'parent-1');
	/** Typed in its thread's field, then Send pressed on it, as the viewer's reply does. */
	function pressedReply(page: Page, { key = replyKey(page), send = REPLY_SEND as DraftSend | null } = {}) {
		const body = 'Agreed, Monday works', tags = page.tags.createMentionSelection(body), drafts = page.drafts.keptCommentDrafts;
		drafts.keepReply(key, body, tags);
		return { key, press: send ? drafts.claim(key, body, tags, undefined, send) : null };
	}

	it.each([['a reload', connect], ['a return to the app', returnToApp]] as const)('after %s, to its thread under the id its press used', async (_name, trigger) => {
		const { key, press } = pressedReply(await open());
		const page = await open();
		await trigger(page);
		expect(wire.posts).toHaveLength(1);
		expect(wire.posts[0]).toMatchObject({ artifactId: 'artifact-1', body: { client_id: press!.clientId, parent_id: 'parent-1', body: 'Agreed, Monday works', thread_id: 'thread-1' } });
		expect(wire.posts[0].body).not.toHaveProperty('anchor_text');
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('on an image’s thread too', async () => {
		const page = await open(), key = replyKey(page, page.drafts.imageDraftKey('image-1'));
		pressedReply(page, { key, send: { artifactId: 'image-1', threadId: null } });
		await connect(page);
		expect(wire.posts.map((post) => [post.artifactId, post.body.parent_id])).toEqual([['image-1', 'parent-1']]);
	});

	it('a reply typed and never pressed, or a chat’s, never goes', async () => {
		const page = await open();
		const { key } = pressedReply(page, { send: null });
		const chat = pressedReply(page, { key: page.drafts.replyDraftKey(page.drafts.CHAT_REPLY_SCOPE, 'parent-1') });
		await connect(page);
		await returnToApp(page);
		page.queue.unsentComments.open(key);
		await page.queue.unsentComments.settled();
		expect(wire.posts).toHaveLength(0);
		expect(page.queue.unsentComments.sending(key)).toBe(false);
		expect(page.queue.unsentComments.sending(chat.key)).toBe(false);
	});

	it('its thread was deleted (404): refused, kept with its words, and a notice names the document', async () => {
		const page = await open(), { key } = pressedReply(page);
		wire.answer.push(refusal(page, 404, null, 'Comment not found'));
		await connect(page);
		await connect(page);
		expect(wire.posts).toHaveLength(1);
		expect(page.drafts.keptCommentDrafts.get(key)).toMatchObject({ body: 'Agreed, Monday works' });
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
		expect(get(page.toast.toasts).map((toast) => toast.message)).toEqual([expect.stringMatching(/^Your comment on notes\.md, “Agreed, Monday works”: /)]);
	});
});

describe('a pressed chat comment or reply goes on its own', () => {
	// Where its box's press keeps it going: the chat, the message by id and seq, and the passage with its whole message around it.
	const CHAT = { threadId: 'thread-1', messageId: 'm1', seq: 3, from_off: 4, to_off: 10, quote: 'Friday', anchor_prefix: 'Due ', anchor_suffix: ' noon' };
	const REPLY = { ...CHAT, root_user_message_id: 'input-1', root_cid: 'cid-1' };
	type Answer = { ok: boolean; status: number; json: () => Promise<unknown> };
	let answers: (Answer | Error)[] = [];
	const chatPosts = vi.fn<(body: Record<string, unknown>) => void>();
	beforeEach(() => {
		answers = []; chatPosts.mockClear();
		vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
			if (!String(url).includes('/chat-comments')) return { ok: true, status: 200, json: async () => ({}) };
			chatPosts(JSON.parse(String(init?.body)));
			const next = answers.shift() ?? { ok: true, status: 200, json: async () => ({ messages: [] }) };
			if (next instanceof Error) throw next;
			return next;
		}));
	});
	afterEach(() => { vi.unstubAllGlobals(); });
	const answer = (status: number, detail: string) => ({ ok: false, status, json: async () => ({ detail, reference: 'ref-9' }) });

	/** Typed on its passage (a reply, in its comment's thread), then Comment or Send pressed on it, as its box does. */
	function pressedChat(page: Page, reply = false) {
		const body = reply ? 'Agreed, Monday works' : 'Move it to Monday please', tags = page.tags.createMentionSelection(body), drafts = page.drafts.keptCommentDrafts;
		const key = reply ? page.drafts.replyDraftKey(page.drafts.CHAT_REPLY_SCOPE, 'comment-1') : page.drafts.chatDraftKey('thread-1', 'm1', CHAT);
		if (reply) drafts.keepReply(key, body, tags);
		else drafts.keep(key, body, tags, { from: 4, to: 10, quote: 'Friday', prefix: 'Due ', suffix: ' noon' });
		return { key, body, press: drafts.claim(key, body, tags, undefined, { chat: reply ? REPLY : CHAT, name: 'Launch plan' }) };
	}

	it.each([['a new comment', false], ['a reply', true]] as const)('%s: after a reload, to the chat on its kept message and passage, under its press’s id, and a second try is the same input', async (_name, reply) => {
		const { key, body, press } = pressedChat(await open(), reply);
		answers.push(new TypeError('Failed to fetch'));
		const page = await open();
		await connect(page);
		expect(page.queue.unsentComments.sending(key)).toBe(true);
		await connect(page);
		expect(chatPosts).toHaveBeenCalledTimes(2);
		const [first, again] = chatPosts.mock.calls.map(([sent]) => sent as { input_id: string; comment: Record<string, unknown> });
		// The server stores one input per id: both tries are the same input, so it is stored once.
		expect([first.input_id, again.input_id]).toEqual([press.clientId, press.clientId]);
		expect(first.comment).toMatchObject({ seq: 3, from_off: 4, to_off: 10, quote: 'Friday', anchor_prefix: 'Due ', anchor_suffix: ' noon', note: body, ...(reply ? { root_user_message_id: 'input-1', root_cid: 'cid-1' } : {}) });
		if (!reply) expect(first.comment.root_cid).toBeUndefined();
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
		expect(get(page.toast.toasts)).toHaveLength(0);
	});

	it.each([
		['its quote is gone', 400, 'comment_quote_gone', 'The text this comment quotes isn’t in the chat'],
		['the chat is deleted', 404, 'chat_not_found', 'This chat is deleted'],
		['the chat’s model is too small', 409, 'comment_model_cap', 'too small a context window'],
		['any other refusal', 400, 'comment_not_sent', 'Couldn’t send this comment. Try again. Reference: ref-9'],
	])('%s: refused for good, kept with its words, and a notice names the chat', async (_name, status, detail, says) => {
		const page = await open(), { key } = pressedChat(page);
		answers.push(answer(status, detail));
		await connect(page);
		await connect(page);
		await returnToApp(page);
		expect(chatPosts).toHaveBeenCalledTimes(1);
		expect(page.drafts.keptCommentDrafts.get(key)).toMatchObject({ body: 'Move it to Monday please' });
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
		expect(page.queue.unsentComments.sending(key)).toBe(false);
		expect(get(page.toast.toasts).map((toast) => toast.message)).toEqual([expect.stringContaining('Your comment on Launch plan, “Move it to Monday please”: ')]);
		expect(get(page.toast.toasts)[0].message).toContain(says);
	});

	it.each([
		['no connection', new TypeError('Failed to fetch')],
		['a server error', answer(500, 'comment_not_stored')],
		['a session to sign in again', answer(401, 'sign_in_required')],
		['too many requests', answer(429, 'too_many')],
	] as const)('%s: says nothing and goes again on the next trigger', async (_name, first) => {
		const page = await open(), { key } = pressedChat(page);
		answers.push(first);
		await connect(page);
		expect(page.queue.unsentComments.sending(key)).toBe(true);
		expect(get(page.toast.toasts)).toHaveLength(0);
		await connect(page);
		expect(chatPosts).toHaveBeenCalledTimes(2);
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it.each([['a titled chat', 'Q3 launch', 'Q3 launch'], ['an untitled one, as the sidebar names it', null, 'New Chat']] as const)('%s: its notice names the chat its press was on, without the box’s word that the text stays there', async (_name, title, named) => {
		const page = await open();
		const { threads } = await import('./threads'), { chatPress, chatCommentDrafts } = await import('$lib/chatCommentDrafts.svelte');
		threads.set(title ? [{ id: 'thread-1', title } as never] : []);
		const drafts = page.drafts.keptCommentDrafts, key = page.drafts.chatDraftKey('thread-1', 'm1', CHAT), tags = page.tags.createMentionSelection('Move it to Monday please');
		drafts.keep(key, 'Move it to Monday please', tags, { from: 4, to: 10, quote: 'Friday', prefix: 'Due ', suffix: ' noon' });
		// What the box's press keeps (`chatPress`), read from the chat's title when Comment is pressed.
		drafts.claim(key, 'Move it to Monday please', tags, undefined, chatPress(CHAT));
		answers.push(answer(400, 'comment_quote_gone'));
		await connect(page);
		const [notice] = get(page.toast.toasts).map((toast) => toast.message);
		expect(notice).toBe(`Your comment on ${named}, “Move it to Monday please”: The text this comment quotes isn’t in the chat, so this comment couldn’t be sent.`);
		// The box keeps its whole line, the text with it.
		expect(chatCommentDrafts.root('thread-1', 'm1', CHAT)?.error).toBe('The text this comment quotes isn’t in the chat, so this comment couldn’t be sent. Your text stays here to copy.');
		threads.set([]);
	});

	it('an edit after a press whose post failed for a reason that passes: no trigger sends, and only the edited text posts, under a fresh id', async () => {
		const page = await open(), { chatCommentDrafts, chatPress } = await import('$lib/chatCommentDrafts.svelte');
		const { postChatComment } = await import('$lib/components/composerSend');
		const send = (note: string) => (inputId: string) => postChatComment({ id: inputId, input_id: inputId, ...CHAT, note } as never, 'thread-1', false, true);
		chatCommentDrafts.setRoot('thread-1', 'm1', CHAT, 'Move it to Monday please');
		answers.push(new TypeError('Failed to fetch'));
		expect(await chatCommentDrafts.postRoot('thread-1', 'm1', CHAT, 'Move it to Monday please', send('Move it to Monday please'), chatPress(CHAT))).toBe(false);
		const key = chatCommentDrafts.rootKey('thread-1', 'm1', CHAT), pressedId = page.drafts.keptCommentDrafts.get(key)!.clientId;
		expect(page.queue.unsentComments.sending(key)).toBe(true);
		chatCommentDrafts.setRoot('thread-1', 'm1', CHAT, 'Move it to Tuesday please');
		await connect(page); await returnToApp(page);
		page.queue.unsentComments.open(key); await page.queue.unsentComments.settled();
		expect(chatPosts).toHaveBeenCalledTimes(1);
		expect(await chatCommentDrafts.postRoot('thread-1', 'm1', CHAT, 'Move it to Tuesday please', send('Move it to Tuesday please'), chatPress(CHAT))).toBe(true);
		const sent = chatPosts.mock.calls.map(([body]) => body as { input_id: string; comment: { note: string } });
		expect(sent.map((body) => body.comment.note)).toEqual(['Move it to Monday please', 'Move it to Tuesday please']);
		expect(sent[1].input_id).not.toBe(pressedId);
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('a box’s own press: a restart passes, and only a chat with no model to answer waits for the person', async () => {
		await open();
		const outcome = await import('$lib/unsentCommentOutcome');
		const { postChatComment } = await import('$lib/components/composerSend');
		const reply = { id: 'c', input_id: 'i', ...CHAT, note: 'n' };
		const restart = await postChatComment(reply as never, 'thread-1', true, true).catch((error: unknown) => error);
		const noModel = await postChatComment(reply as never, 'thread-1', false, false).catch((error: unknown) => error);
		expect([outcome.unsentOutcome(restart), outcome.unsentOutcome(noModel)]).toEqual(['retry', 'refused']);
		expect(chatPosts).not.toHaveBeenCalled();
	});

	it('a typed chat comment, never pressed, never goes', async () => {
		const page = await open(), tags = page.tags.createMentionSelection('Typed only');
		const key = page.drafts.chatDraftKey('thread-1', 'm1', CHAT);
		page.drafts.keptCommentDrafts.keep(key, 'Typed only', tags, { from: 4, to: 10, quote: 'Friday', prefix: 'Due ', suffix: ' noon' });
		await connect(page); await returnToApp(page);
		page.queue.unsentComments.open(key); await page.queue.unsentComments.settled();
		expect(chatPosts).not.toHaveBeenCalled();
		expect(page.drafts.keptCommentDrafts.get(key)?.body).toBe('Typed only');
	});
});
