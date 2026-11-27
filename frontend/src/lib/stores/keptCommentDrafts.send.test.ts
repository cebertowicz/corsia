// A kept draft holds its passage and, once pressed, what its post needs, and both outlive the page. Each "page" is a fresh
// import of the store over the same device storage, as a reload or a relaunch leaves it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DRAFT_STORAGE_PREFIX } from './keptCommentDraftsStorage';

type Page = { drafts: typeof import('./keptCommentDrafts.svelte'); auth: typeof import('./auth'); tags: typeof import('$lib/components/comments/mentionSelection') };
let current: Page | null = null;

async function open(): Promise<Page> {
	vi.resetModules();
	const page = { drafts: await import('./keptCommentDrafts.svelte'), auth: await import('./auth'), tags: await import('$lib/components/comments/mentionSelection') };
	page.auth.user.set({ id: 'person-1', name: 'person-1' });
	return current = page;
}
async function relaunch(): Promise<Page> {
	current?.drafts.keptCommentDrafts.reset();
	return open();
}
const KEY = (page: Page) => page.drafts.documentDraftKey({ projectId: 'project-1', path: 'notes.md' });
const PLACE = { from: 60, to: 66, quote: 'Friday', prefix: 'll a side.\n\nThe launch moves to ', suffix: '.\nNotes follow on the next line ' };
const SEND = { artifactId: 'artifact-1', threadId: 'thread-1' };

/** The draft typed on its passage, then Comment pressed on it: the press keeps what its post needs. */
function pressed(page: Page, body = 'Move it to Monday') {
	const tags = page.tags.createMentionSelection(body), drafts = page.drafts.keptCommentDrafts, key = KEY(page);
	drafts.keep(key, body, tags, PLACE);
	return { key, tags, press: drafts.claim(key, body, tags, undefined, SEND) };
}

beforeEach(() => localStorage.clear());
afterEach(() => { current?.drafts.keptCommentDrafts.reset(); current = null; localStorage.clear(); });

describe('a kept draft keeps its passage and its send on this device', () => {
	it('comes back after a reload with both', async () => {
		const { press } = pressed(await open());
		const page = await relaunch(), draft = page.drafts.keptCommentDrafts.get(KEY(page));
		expect(draft).toMatchObject({ body: 'Move it to Monday', clientId: press.clientId, place: PLACE, send: SEND });
	});

	it('reads an entry from before drafts kept a passage as it did', async () => {
		localStorage.setItem(`${DRAFT_STORAGE_PREFIX}person-1:${KEY(await open())}`, JSON.stringify({ body: 'Old words', tags: [], clientId: 'c-1' }));
		const page = await relaunch(), draft = page.drafts.keptCommentDrafts.get(KEY(page));
		expect(draft).toMatchObject({ body: 'Old words', clientId: 'c-1' });
		expect(draft?.place).toBeUndefined();
		expect(draft?.send).toBeUndefined();
	});

	it.each([
		['a place with no quote', { place: { ...PLACE, quote: '' } }],
		['a place that runs backwards', { place: { ...PLACE, from: 70 } }],
		['a send with no document', { send: { artifactId: '', threadId: null } }],
		['a send that is not one', { send: 'artifact-1' }],
	])('drops %s alone and keeps the words', async (_name, field) => {
		localStorage.setItem(`${DRAFT_STORAGE_PREFIX}person-1:${KEY(await open())}`, JSON.stringify({ body: 'Kept words', tags: [], place: PLACE, send: SEND, ...field }));
		const page = await relaunch(), draft = page.drafts.keptCommentDrafts.get(KEY(page));
		expect(draft?.body).toBe('Kept words');
		const dropped = Object.keys(field)[0] as 'place' | 'send';
		expect(draft?.[dropped]).toBeUndefined();
		expect(draft?.[dropped === 'place' ? 'send' : 'place']).toEqual(dropped === 'place' ? SEND : PLACE);
	});
});

describe('only pressed words are sent without the person', () => {
	it('an edit after a press makes the draft typed again, and its id stays for the next press', async () => {
		const page = await open(), drafts = page.drafts.keptCommentDrafts;
		const { key, tags, press } = pressed(page);
		drafts.fail(key, 'Move it to Monday', tags, 'Couldn’t reach CORSIA.');
		drafts.keep(key, 'Move it to Tuesday', tags);
		expect(drafts.get(key)).toMatchObject({ body: 'Move it to Tuesday', clientId: press.clientId, place: PLACE });
		expect(drafts.get(key)?.send).toBeUndefined();
		expect((await relaunch()).drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
	});

	it('the same text kept again is no edit: the draft stays pressed', async () => {
		const page = await open(), drafts = page.drafts.keptCommentDrafts;
		const { key, tags } = pressed(page);
		drafts.keep(key, 'Move it to Monday', tags);
		expect(drafts.get(key)?.send).toEqual(SEND);
	});

	it('an edit while its post is out: the post stores the pressed words, the edit stays as a typed draft', async () => {
		const page = await open(), drafts = page.drafts.keptCommentDrafts;
		const { key, tags } = pressed(page);
		drafts.keep(key, 'Move it to Tuesday', tags);
		expect(drafts.posted(key, 'Move it to Monday', [])).toBe(false);
		expect(drafts.get(key)).toMatchObject({ body: 'Move it to Tuesday', place: PLACE });
		expect(drafts.get(key)?.send).toBeUndefined();
		expect(drafts.get(key)?.clientId).toBeUndefined();
	});

	it('a press after the edit posts under the same id, so the server reconciles it with the earlier press', async () => {
		const page = await open(), drafts = page.drafts.keptCommentDrafts;
		const { key, tags, press } = pressed(page);
		drafts.keep(key, 'Move it to Tuesday', tags);
		const again = drafts.claim(key, 'Move it to Tuesday', tags, undefined, SEND);
		expect(again).toEqual({ clientId: press.clientId, again: true });
		expect(drafts.get(key)?.send).toEqual(SEND);
	});

	it('when the earlier version was the one stored, the changes kept are not sent on their own', async () => {
		const page = await open(), drafts = page.drafts.keptCommentDrafts;
		const { key, tags } = pressed(page);
		drafts.fail(key, 'Move it to Tuesday', tags, 'Your earlier version was posted. Your changes are kept here.', true);
		expect(drafts.get(key)?.send).toBeUndefined();
	});

	it('a refusal is never sent again without the person, and the words and passage stay', async () => {
		const page = await open(), drafts = page.drafts.keptCommentDrafts;
		const { key } = pressed(page);
		drafts.unsend(key);
		expect(drafts.get(key)).toMatchObject({ body: 'Move it to Monday', place: PLACE });
		expect(drafts.get(key)?.send).toBeUndefined();
	});
});

describe('a box says where its draft is', () => {
	it('moves it to the passage the box is on, removes it for none, and leaves it when the box does not know', async () => {
		const page = await open(), drafts = page.drafts.keptCommentDrafts, key = KEY(page);
		const tags = page.tags.createMentionSelection('Words');
		drafts.keep(key, 'Words', tags, PLACE);
		drafts.keep(key, 'Words!', tags);
		expect(drafts.get(key)?.place).toEqual(PLACE);
		drafts.keep(key, 'Words!', tags, null);
		expect(drafts.get(key)?.place).toBeUndefined();
	});
});
