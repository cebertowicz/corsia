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
