// A kept draft outlives the page: each "page" here is a fresh import of the store over the same device storage,
// which is what a reload, a quit and relaunch, and an update restart leave behind.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Page = {
	drafts: typeof import('./keptCommentDrafts.svelte');
	auth: typeof import('./auth');
	tags: typeof import('$lib/components/comments/mentionSelection');
	toast: typeof import('./toast');
};

let current: Page | null = null;

/** A new page over the same device storage, with `userId` signed in (or nobody yet). */
async function open(userId: string | null = 'person-1'): Promise<Page> {
	vi.resetModules();
	const page = {
		drafts: await import('./keptCommentDrafts.svelte'),
		auth: await import('./auth'),
		tags: await import('$lib/components/comments/mentionSelection'),
		toast: await import('./toast'),
	};
	if (userId) page.auth.user.set({ id: userId, name: userId });
	current = page;
	return page;
}
/** The page is left (reload, quit, update restart, a crash) and opened again: nothing runs on the way out. */
async function relaunch(userId: string | null = 'person-1'): Promise<Page> {
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
		page.drafts.keptCommentDrafts[keep](keyOf(page), '', tags);
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(keyOf(page))).toBeUndefined();
	});
});

describe('Cancel', () => {
	it('clears the device at once', async () => {
		let page = await open();
		const key = documentKey(page), { body, tags } = tagged(page);
		page.drafts.keptCommentDrafts.keep(key, body, tags);
		page.drafts.keptCommentDrafts.keep(key, `${body} more`, tags);
		page.drafts.keptCommentDrafts.forget(key);
		expect(localStorage.length).toBe(0);
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
	});
});

describe('writing', () => {
	it('writes each change at once, and nothing more for a caret move', async () => {
		const page = await open();
		const key = documentKey(page), { body, tags } = tagged(page);
		const setItem = vi.spyOn(localStorage, 'setItem');
		page.drafts.keptCommentDrafts.keep(key, 'A', tags);
		expect(setItem).toHaveBeenCalledTimes(1);
		expect(JSON.parse(localStorage.getItem(localStorage.key(0)!)!).body).toBe('A');
		page.drafts.keptCommentDrafts.keep(key, body, tags);
		expect(setItem).toHaveBeenCalledTimes(2);
		expect(JSON.parse(localStorage.getItem(localStorage.key(0)!)!).body).toBe(body);
		page.drafts.keptCommentDrafts.select(key, [1, 1]);
		expect(setItem).toHaveBeenCalledTimes(2);
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
		const setItem = vi.spyOn(localStorage, 'setItem');
		await page.auth.logout();
		expect(page.drafts.keptCommentDrafts.get(documentKey(page))).toBeUndefined();
		// Every other window hears whose drafts went, and nothing of the signal stays stored.
		expect(setItem).toHaveBeenCalledWith('corsia:commentDraftSignOut:v1', 'person-1');
		expect(localStorage.getItem('corsia:commentDraftSignOut:v1')).toBeNull();
		setItem.mockRestore();
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

describe('a draft this device does not keep', () => {
	const stored = (key: string) => localStorage.getItem(`corsia:commentDraft:v1:person-1:${key}`);
	const toastsUp = (page: Page) => { let list: { message: string }[] = []; page.toast.toasts.subscribe((value) => (list = value))(); return list.map((toast) => toast.message); };
	/** A box showing the draft, laid out inside the window (jsdom lays nothing out). */
	function boxOnScreen(page: Page, key: string): () => void {
		const node = document.createElement('div');
		node.getBoundingClientRect = () => new DOMRect(20, 100, 300, 80);
		document.body.append(node);
		const drop = page.drafts.keptCommentDrafts.shown(key, node);
		return () => { drop(); node.remove(); };
	}

	it('too long: stays in this page, its stored earlier version goes, its box says why, and nothing else kept is lost', async () => {
		let page = await open();
		const key = documentKey(page), other = page.drafts.imageDraftKey('artifact-7');
		page.drafts.keptCommentDrafts.keep(other, 'another draft', page.tags.createMentionSelection('another draft'));
		page.drafts.keptCommentDrafts.keep(key, 'short version', page.tags.createMentionSelection('short version'));
		expect(page.drafts.keptCommentDrafts.notKept(key)).toBe('');
		const close = boxOnScreen(page, key);
		const huge = 'x'.repeat(200_001);
		page.drafts.keptCommentDrafts.keep(key, huge, page.tags.createMentionSelection(huge));
		expect(page.drafts.keptCommentDrafts.get(key)?.body).toBe(huge);
		expect(stored(key)).toBeNull();
		expect(page.drafts.keptCommentDrafts.notKept(key)).toBe('Too long to keep on this device. Post it or copy it before you close CORSIA.');
		// Its box is on screen, so the line under it is the one place this is said.
		expect(toastsUp(page)).toEqual([]);
		close();
		page = await relaunch();
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
		expect(page.drafts.keptCommentDrafts.get(other)?.body).toBe('another draft');
	});

	it.each([
		['document', (page: Page) => documentKey(page)],
		['image', (page: Page) => page.drafts.imageDraftKey('artifact-7')],
		['reply', (page: Page) => page.drafts.replyDraftKey(documentKey(page), 'comment-1')],
		['chat', (page: Page) => page.drafts.chatDraftKey('thread-1', 'message-1', { from_off: 0, to_off: 4 })],
		['chat reply', (page: Page) => page.drafts.replyDraftKey(page.drafts.CHAT_REPLY_SCOPE, 'comment-1')],
	])('%s: a press that cannot be kept is never queued for automatic sending', async (_surface, keyOf) => {
		const page = await open();
		const key = keyOf(page), body = 'x'.repeat(200_001), tags = page.tags.createMentionSelection(body);
		page.drafts.keptCommentDrafts.keep(key, body, tags);
		const press = page.drafts.keptCommentDrafts.claim(key, body, tags, undefined, { artifactId: 'artifact-7', threadId: 'thread-1' });
		expect(page.drafts.keptCommentDrafts.notKept(key)).not.toBe('');
		expect(page.drafts.keptCommentDrafts.get(key)?.clientId).toBe(press.clientId);
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
		expect(page.drafts.keptCommentDrafts.pressed()).not.toContain(key);
	});

	it('clears the not-kept warning when a pressed draft is emptied', async () => {
		const page = await open(), key = documentKey(page), body = 'x'.repeat(200_001);
		const tags = page.tags.createMentionSelection(body);
		page.drafts.keptCommentDrafts.keep(key, body, tags);
		const press = page.drafts.keptCommentDrafts.claim(key, body, tags);
		expect(page.drafts.keptCommentDrafts.notKept(key)).not.toBe('');
		page.drafts.keptCommentDrafts.keep(key, '', page.tags.createMentionSelection());
		expect(page.drafts.keptCommentDrafts.get(key)?.clientId).toBe(press.clientId);
		expect(page.drafts.keptCommentDrafts.notKept(key)).toBe('');
	});

	it('a send added after the press that crosses the storage cap is not queued', async () => {
		const page = await open(), key = documentKey(page), body = 'x'.repeat(199_700);
		const tags = page.tags.createMentionSelection(body);
		page.drafts.keptCommentDrafts.keep(key, body, tags);
		const press = page.drafts.keptCommentDrafts.claim(key, body, tags);
		expect(page.drafts.keptCommentDrafts.notKept(key)).toBe('');
		page.drafts.keptCommentDrafts.sends(key, press.clientId, body, { artifactId: 'artifact-7', threadId: 'thread-1', name: 'n'.repeat(500) });
		expect(page.drafts.keptCommentDrafts.get(key)?.send).toBeUndefined();
		expect(page.drafts.keptCommentDrafts.pressed()).not.toContain(key);
	});

	it('the line goes once a later change is kept, and when the draft leaves this page', async () => {
		const page = await open();
		const key = documentKey(page), huge = 'x'.repeat(200_001);
		page.drafts.keptCommentDrafts.keep(key, huge, page.tags.createMentionSelection(huge));
		expect(page.drafts.keptCommentDrafts.notKept(key)).not.toBe('');
		page.drafts.keptCommentDrafts.keep(key, 'shorter', page.tags.createMentionSelection('shorter'));
		expect(page.drafts.keptCommentDrafts.notKept(key)).toBe('');
		expect(JSON.parse(stored(key)!).body).toBe('shorter');
		page.drafts.keptCommentDrafts.keep(key, huge, page.tags.createMentionSelection(huge));
		page.drafts.keptCommentDrafts.forget(key);
		expect(page.drafts.keptCommentDrafts.notKept(key)).toBe('');
	});

	it('with no box of it on screen, a toast says why, and goes once its box comes on screen', async () => {
		const page = await open();
		const key = documentKey(page), huge = 'x'.repeat(200_001);
		page.drafts.keptCommentDrafts.keep(key, huge, page.tags.createMentionSelection(huge));
		expect(toastsUp(page)).toEqual(['Too long to keep on this device. Post it or copy it before you close CORSIA.']);
		// Told once: a further change that is still not kept raises no second toast.
		page.drafts.keptCommentDrafts.keep(key, `${huge}y`, page.tags.createMentionSelection(`${huge}y`));
		expect(toastsUp(page)).toHaveLength(1);
		const close = boxOnScreen(page, key);
		expect(toastsUp(page)).toEqual([]);
		expect(page.drafts.keptCommentDrafts.notKept(key)).not.toBe('');
		close();
	});

	it('once the drafts kept fill their share, a new one stays in this page with its line, and the others stay kept', async () => {
		let page = await open();
		const long = 'y'.repeat(199_000), tags = page.tags.createMentionSelection(long);
		const keys = [...Array(11).keys()].map((index) => page.drafts.imageDraftKey(`artifact-${index}`));
		for (const key of keys) page.drafts.keptCommentDrafts.keep(key, long, tags);
		expect(localStorage.length).toBe(10);
		expect(page.drafts.keptCommentDrafts.notKept(keys[10])).toBe('This device has no room left to keep this draft. Post it or copy it before you close CORSIA.');
		expect(keys.slice(0, 10).map((key) => page.drafts.keptCommentDrafts.notKept(key))).toEqual(Array(10).fill(''));
		page = await relaunch();
		expect(keys.filter((key) => page.drafts.keptCommentDrafts.get(key)?.body === long)).toHaveLength(10);
	});

	it('a device that refuses the write: the draft stays in this page, its stored earlier version goes, and its line says so', async () => {
		const page = await open();
		const key = documentKey(page);
		page.drafts.keptCommentDrafts.keep(key, 'kept there', page.tags.createMentionSelection('kept there'));
		expect(stored(key)).not.toBeNull();
		const setItem = vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError'); });
		page.drafts.keptCommentDrafts.keep(key, 'kept here', page.tags.createMentionSelection('kept here'));
		expect(page.drafts.keptCommentDrafts.get(key)?.body).toBe('kept here');
		expect(stored(key)).toBeNull();
		expect(page.drafts.keptCommentDrafts.notKept(key)).toBe('Couldn’t keep this draft on this device. Post it or copy it before you close CORSIA.');
		setItem.mockRestore();
		page.drafts.keptCommentDrafts.keep(key, 'kept again', page.tags.createMentionSelection('kept again'));
		expect(page.drafts.keptCommentDrafts.notKept(key)).toBe('');
		expect(JSON.parse(stored(key)!).body).toBe('kept again');
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
		localStorage.setItem(`corsia:commentDraft:v1:person-2:${key}:other`, '{"body":7}');
		const next = await relaunch();
		expect(next.drafts.keptCommentDrafts.get(key)).toBeUndefined();
		// Signing in removes what no version can read, anyone's, so it does not keep its room; another person's readable draft stays.
		expect(localStorage.getItem(`corsia:commentDraft:v1:person-1:${key}`)).toBeNull();
		expect(localStorage.getItem(`corsia:commentDraft:v1:person-2:${key}:other`)).toBeNull();
		expect(JSON.parse(localStorage.getItem(`corsia:commentDraft:v1:person-2:${key}`)!).body).toBe('not yours');
	});

	it('a Sign out in another window takes the person’s drafts from this one too, even one being typed', async () => {
		const page = await open();
		const key = documentKey(page), reply = page.drafts.replyDraftKey(key, 'comment-1');
		page.drafts.keptCommentDrafts.keep(key, 'a comment', page.tags.createMentionSelection('a comment'));
		page.drafts.keptCommentDrafts.keepReply(reply, 'mine here', page.tags.createMentionSelection('mine here'));
		const box = document.createElement('div'), field = document.createElement('textarea');
		box.append(field); document.body.append(box);
		const drop = page.drafts.keptCommentDrafts.shown(reply, box);
		field.focus();
		// What the other window's Sign out leaves: its entries removed, then the sign-out set and removed.
		localStorage.clear();
		window.dispatchEvent(new StorageEvent('storage', { key: 'corsia:commentDraftSignOut:v1', newValue: 'person-1' }));
		window.dispatchEvent(new StorageEvent('storage', { key: 'corsia:commentDraftSignOut:v1', newValue: null }));
		expect(page.drafts.keptCommentDrafts.get(key)).toBeUndefined();
		expect(page.drafts.keptCommentDrafts.get(reply)).toBeUndefined();
		// Nothing more is kept on the device for them from this window.
		page.drafts.keptCommentDrafts.keepReply(reply, 'mine here, more', page.tags.createMentionSelection('mine here, more'));
		expect(localStorage.length).toBe(0);
		drop(); box.remove();
	});

	it('another person’s Sign out in another window leaves this person’s drafts', async () => {
		const page = await open();
		const key = documentKey(page);
		page.drafts.keptCommentDrafts.keep(key, 'a comment', page.tags.createMentionSelection('a comment'));
		window.dispatchEvent(new StorageEvent('storage', { key: 'corsia:commentDraftSignOut:v1', newValue: 'person-2' }));
		expect(page.drafts.keptCommentDrafts.get(key)?.body).toBe('a comment');
		expect(localStorage.length).toBe(1);
	});
});
