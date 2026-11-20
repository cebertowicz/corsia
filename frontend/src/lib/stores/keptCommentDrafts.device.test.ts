// A kept draft outlives the page: each "page" here is a fresh import of the store over the same device storage,
// which is what a reload, a quit and relaunch, and an update restart leave behind.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Page = {
	drafts: typeof import('./keptCommentDrafts.svelte');
	auth: typeof import('./auth');
	tags: typeof import('$lib/components/comments/mentionSelection');
};

let current: Page | null = null;

/** A new page over the same device storage, with `userId` signed in (or nobody yet). */
async function open(userId: string | null = 'person-1'): Promise<Page> {
	vi.resetModules();
	const page = {
		drafts: await import('./keptCommentDrafts.svelte'),
		auth: await import('./auth'),
		tags: await import('$lib/components/comments/mentionSelection'),
	};
	if (userId) page.auth.user.set({ id: userId, name: userId });
	current = page;
	return page;
}
/** The page is left (reload, quit, update restart) and opened again. */
async function relaunch(userId: string | null = 'person-1'): Promise<Page> {
	window.dispatchEvent(new Event('pagehide'));
	current?.drafts.keptCommentDrafts.reset();
	return open(userId);
}
/** Text tagging one agent, as a box holds it once the agent was picked. */
function tagged(page: Page, body = 'Ask @Ada about it') {
	const tags = page.tags.createMentionSelection(body);
	tags.select('agent-ada', body.indexOf('@Ada'), '@Ada');
	return { body, tags };
}

const documentKey = (page: Page) => page.drafts.documentDraftKey({ projectId: 'project-1', path: 'notes.md' });
const surfaces = [
	['a document’s new comment', (page: Page) => documentKey(page), 'keep'],
	['an image’s new comment', (page: Page) => page.drafts.imageDraftKey('artifact-7'), 'keep'],
	['a document reply', (page: Page) => page.drafts.replyDraftKey(documentKey(page), 'comment-1'), 'keepReply'],
] as const;

beforeEach(() => localStorage.clear());
afterEach(() => {
	current?.drafts.keptCommentDrafts.reset();
	current = null;
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe.each(surfaces)('%s outlives the page', (_name, keyOf, keep) => {
	it('typed, then the page is left: the text and its tagged agents come back', async () => {
		let page = await open();
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts[keep](keyOf(page), body, tags);
		page = await relaunch();
		const draft = page.drafts.keptCommentDrafts.get(keyOf(page));
		expect(draft?.body).toBe(body);
		expect(draft?.mentionSelection.occurrences(body)).toEqual([{ agent_id: 'agent-ada', start: 4, end: 8 }]);
		// Neither the caret nor a failure's reason is kept on the device.
		expect(draft?.selection).toBeUndefined();
		expect(draft?.error).toBeUndefined();
	});

	it('posted, then relaunched: nothing comes back', async () => {
		let page = await open();
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts[keep](keyOf(page), body, tags);
		page.drafts.keptCommentDrafts.claim(keyOf(page), body, tags);
		window.dispatchEvent(new Event('pagehide'));
		expect(page.drafts.keptCommentDrafts.posted(keyOf(page), body, ['agent-ada'])).toBe(true);
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(keyOf(page))).toBeUndefined();
	});

	it('left while its post is in flight: it comes back under the same id, so posting again cannot store it twice', async () => {
		let page = await open();
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts[keep](keyOf(page), body, tags);
		const press = page.drafts.keptCommentDrafts.claim(keyOf(page), body, tags);
		page = await relaunch();
		const draft = page.drafts.keptCommentDrafts.get(keyOf(page))!;
		expect(draft.body).toBe(body);
		expect(page.drafts.keptCommentDrafts.claim(keyOf(page), draft.body, draft.mentionSelection)).toEqual({ clientId: press.clientId, again: true });
	});

	it('emptied, then relaunched: nothing comes back', async () => {
		let page = await open();
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts[keep](keyOf(page), body, tags);
		window.dispatchEvent(new Event('pagehide'));
		page.drafts.keptCommentDrafts[keep](keyOf(page), '', tags);
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(keyOf(page))).toBeUndefined();
	});
});

describe('Cancel', () => {
	it('clears the device at once, even with a write still waiting', async () => {
		let page = await open();
		const key = documentKey(page), { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts.keep(key, body, tags);
		window.dispatchEvent(new Event('pagehide'));
		page.drafts.keptCommentDrafts.keep(key, `${body} more`, tags);
		page.drafts.keptCommentDrafts.forget(key);
		expect(localStorage.length).toBe(0);
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});
});

describe('writing', () => {
	it('waits for typing to pause, and writes nothing more for a caret move', async () => {
		vi.useFakeTimers();
		const page = await open();
		const key = documentKey(page), { body, tags } = tagged(page);
		const setItem = vi.spyOn(localStorage, 'setItem');
		page.drafts.keptCommentDrafts.keep(key, 'A', tags);
		page.drafts.keptCommentDrafts.keep(key, body, tags);
		expect(localStorage.length).toBe(0);
		vi.advanceTimersByTime(300);
		expect(setItem).toHaveBeenCalledTimes(1);
		expect(JSON.parse(localStorage.getItem(localStorage.key(0)!)!).body).toBe(body);
		const before = setItem.mock.calls.length;
		page.drafts.keptCommentDrafts.select(key, [1, 1]);
		vi.advanceTimersByTime(300);
		expect(setItem.mock.calls.length).toBe(before);
	});

	it('writes when the page is hidden, without waiting', async () => {
		vi.useFakeTimers();
		const page = await open();
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts.keep(documentKey(page), body, tags);
		Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
		document.dispatchEvent(new Event('visibilitychange'));
		Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
		expect(localStorage.length).toBe(1);
	});

	it('keeps under the signed-in person a draft typed before anyone was signed in', async () => {
		let page = await open(null);
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts.keep(documentKey(page), body, tags);
		page.auth.user.set({ id: 'person-1', name: 'person-1' });
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))?.body).toBe(body);
	});
});

describe('signing out', () => {
	it('the person’s own Sign out clears their drafts, here and on the device; another person’s stay', async () => {
		let page = await open('person-2');
		page.drafts.keptCommentDrafts.keep(documentKey(page), 'theirs', page.tags.createMentionSelection('theirs'));
		page = await relaunch('person-1');
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts.keep(documentKey(page), body, tags);
		page.drafts.keptCommentDrafts.keepReply(page.drafts.replyDraftKey(documentKey(page), 'comment-1'), body, tags);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) })));
		await page.auth.logout();
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))).toBeUndefined();
		page.auth.user.set({ id: 'person-1', name: 'person-1' });
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))).toBeUndefined();
		page = await relaunch('person-1');
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))).toBeUndefined();
		expect(page.drafts.keptCommentDrafts.get(page.drafts.replyDraftKey(documentKey(page), 'comment-1'))).toBeUndefined();
		page = await relaunch('person-2');
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))?.body).toBe('theirs');
	});

	it('an expired session keeps them: the same person signing in again finds them', async () => {
		let page = await open();
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts.keep(documentKey(page), body, tags);
		page.auth.clearExpiredSession();
		page.auth.user.set({ id: 'person-1', name: 'person-1' });
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))?.body).toBe(body);
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))?.body).toBe(body);
	});

	it('a session the server ends after a key change keeps them, as an expired one does', async () => {
		let page = await open();
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts.keep(documentKey(page), body, tags);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403, json: async () => ({ detail: { code: 'signed_out' } }) })));
		const keys = await import('./keys');
		await expect(keys.listKeys()).rejects.toMatchObject({ code: 'signed_out' });
		const { get } = await import('svelte/store');
		expect(get(page.auth.user)).toBeNull();
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))?.body).toBe(body);
	});

	it('another person signing in here sees none of the last one’s drafts, and theirs are still kept', async () => {
		let page = await open();
		const { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts.keep(documentKey(page), body, tags);
		page.auth.clearExpiredSession();
		page.auth.user.set({ id: 'person-2', name: 'person-2' });
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))).toBeUndefined();
		page = await relaunch('person-1');
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))?.body).toBe(body);
	});
});

describe('the cap on drafts kept on the device', () => {
	it('a draft too long to keep stays in this page with a warning, and nothing kept already is lost', async () => {
		let page = await open();
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const key = documentKey(page), other = page.drafts.imageDraftKey('artifact-7');
		page.drafts.keptCommentDrafts.keep(other, 'another draft', page.tags.createMentionSelection('another draft'));
		page.drafts.keptCommentDrafts.keep(key, 'short version', page.tags.createMentionSelection('short version'));
		window.dispatchEvent(new Event('pagehide'));
		const huge = 'x'.repeat(200_001);
		page.drafts.keptCommentDrafts.keep(key, huge, page.tags.createMentionSelection(huge));
		window.dispatchEvent(new Event('pagehide'));
		expect(page.drafts.keptCommentDrafts.get(key)?.body).toBe(huge);
		expect(warn).toHaveBeenCalledTimes(1);
		expect(String(warn.mock.calls[0][0])).toContain('kept in this window only');
		expect(String(warn.mock.calls[0][0])).not.toContain('xxx');
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(key)?.body).toBe('short version');
		expect(page.drafts.keptCommentDrafts.get(other)?.body).toBe('another draft');
	});

	it('once the drafts kept fill their share, a new one stays in this page and the others stay kept', async () => {
		let page = await open();
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const long = 'y'.repeat(199_000), tags = page.tags.createMentionSelection(long);
		const keys = [...Array(11).keys()].map((index) => page.drafts.imageDraftKey(`artifact-${index}`));
		for (const key of keys) page.drafts.keptCommentDrafts.keep(key, long, tags);
		window.dispatchEvent(new Event('pagehide'));
		expect(warn).toHaveBeenCalledTimes(1);
		const keptCount = localStorage.length;
		expect(keptCount).toBe(10);
		page = await relaunch();
		expect(keys.filter((key) => page.drafts.keptCommentDrafts.get(key)?.body === long)).toHaveLength(10);
	});

	it('a device that refuses the write keeps the draft in this page with a warning', async () => {
		const page = await open();
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
		page.drafts.keptCommentDrafts.keep(documentKey(page), 'kept here', page.tags.createMentionSelection('kept here'));
		window.dispatchEvent(new Event('pagehide'));
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))?.body).toBe('kept here');
		expect(warn).toHaveBeenCalledTimes(1);
	});
});

describe('two windows', () => {
	/** What another window of the same app wrote under the signed-in person. */
	function written(page: Page, key: string, value: string | null): void {
		const name = `corsia:commentDraft:v1:person-1:${key}`;
		if (value === null) localStorage.removeItem(name); else localStorage.setItem(name, value);
		window.dispatchEvent(new StorageEvent('storage', { key: name, newValue: value }));
	}

	it('the draft follows what the other window typed and removed', async () => {
		const page = await open();
		const key = page.drafts.replyDraftKey(documentKey(page), 'comment-1');
		written(page, key, JSON.stringify({ body: 'Ask @Ada there', tags: [{ agent_id: 'agent-ada', start: 4, end: 8 }] }));
		const draft = page.drafts.keptCommentDrafts.get(key);
		expect(draft?.body).toBe('Ask @Ada there');
		expect(draft?.mentionSelection.occurrences('Ask @Ada there')).toEqual([{ agent_id: 'agent-ada', start: 4, end: 8 }]);
		written(page, key, null);
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('the draft holds still while the person is typing in its box here', async () => {
		const page = await open();
		const key = page.drafts.replyDraftKey(documentKey(page), 'comment-1');
		page.drafts.keptCommentDrafts.keepReply(key, 'mine here', page.tags.createMentionSelection('mine here'));
		const box = document.createElement('div'), field = document.createElement('textarea');
		box.append(field); document.body.append(box);
		const drop = page.drafts.keptCommentDrafts.shown(key, box);
		field.focus();
		written(page, key, JSON.stringify({ body: 'from the other window', tags: [] }));
		expect(page.drafts.keptCommentDrafts.get(key)?.body).toBe('mine here');
		field.blur();
		written(page, key, JSON.stringify({ body: 'from the other window again', tags: [] }));
		expect(page.drafts.keptCommentDrafts.get(key)?.body).toBe('from the other window again');
		drop(); box.remove();
	});

	it('ignores what another person kept, and an entry it cannot read', async () => {
		const page = await open();
		const key = documentKey(page);
		localStorage.setItem(`corsia:commentDraft:v1:person-2:${key}`, JSON.stringify({ body: 'not yours', tags: [] }));
		window.dispatchEvent(new StorageEvent('storage', { key: `corsia:commentDraft:v1:person-2:${key}`, newValue: JSON.stringify({ body: 'not yours', tags: [] }) }));
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
		localStorage.setItem(`corsia:commentDraft:v1:person-1:${key}`, '{not json');
		const next = await relaunch();
		expect(next.drafts.keptCommentDrafts.get(key)).toBeUndefined();
		// Left where it was: nothing kept is dropped.
		expect(localStorage.getItem(`corsia:commentDraft:v1:person-1:${key}`)).toBe('{not json');
	});
});
