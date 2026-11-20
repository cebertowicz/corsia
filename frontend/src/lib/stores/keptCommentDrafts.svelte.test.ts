import { afterEach, describe, expect, it } from 'vitest';
import { flushSync } from 'svelte';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { documentDraftKey, keptCommentDrafts, replyDraftKey, showsDraft } from './keptCommentDrafts.svelte';

afterEach(() => keptCommentDrafts.reset());

const scope = documentDraftKey({ projectId: 'project-1', path: 'notes.md' });

describe('a reply kept while it is typed', () => {
	it('keeps its text, tags and caret until its field is emptied', () => {
		const key = replyDraftKey(scope, 'comment-1');
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'half a reply', tags);
		keptCommentDrafts.select(key, [2, 6]);
		const kept = keptCommentDrafts.get(key)!;
		expect(kept.body).toBe('half a reply');
		expect(kept.mentionSelection).toBe(tags);
		expect(kept.selection).toEqual([2, 6]);
		// A reply has no Cancel: emptying its field clears it, its id and reason included.
		keptCommentDrafts.claim(key, 'half a reply', tags);
		keptCommentDrafts.fail(key, 'half a reply', tags, 'Couldn’t post');
		keptCommentDrafts.keepReply(key, '', tags);
		expect(keptCommentDrafts.get(key)).toBeUndefined();
	});

	it('keeps each thread’s reply apart, and a document’s apart from another’s', () => {
		const other = documentDraftKey({ projectId: 'project-1', path: 'other.md' });
		keptCommentDrafts.keepReply(replyDraftKey(scope, 'comment-1'), 'one', createMentionSelection());
		keptCommentDrafts.keepReply(replyDraftKey(scope, 'comment-2'), 'two', createMentionSelection());
		expect(keptCommentDrafts.get(replyDraftKey(scope, 'comment-1'))?.body).toBe('one');
		expect(keptCommentDrafts.get(replyDraftKey(scope, 'comment-2'))?.body).toBe('two');
		expect(keptCommentDrafts.get(replyDraftKey(other, 'comment-1'))).toBeUndefined();
	});

	it('keeps the reason a post failed, with the text, caret and the one id every press posts under', () => {
		const key = replyDraftKey(scope, 'comment-1');
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'half a reply', tags);
		keptCommentDrafts.select(key, [4, 4]);
		const first = keptCommentDrafts.claim(key, 'half a reply', tags);
		expect(first.again).toBe(false);
		keptCommentDrafts.fail(key, 'half a reply', tags, 'Couldn’t post the comment — try again');
		expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'half a reply', error: 'Couldn’t post the comment — try again', clientId: first.clientId, selection: [4, 4] });
		keptCommentDrafts.clearError(key);
		expect(keptCommentDrafts.claim(key, 'half a reply', tags)).toEqual({ clientId: first.clientId, again: true });
		// Its earlier version was stored under that id: the changes post as a new reply, under a fresh id.
		keptCommentDrafts.fail(key, 'half a reply, changed', tags, 'Your earlier version was posted. Your changes are kept here.', true);
		const fresh = keptCommentDrafts.claim(key, 'half a reply, changed', tags);
		expect(fresh.again).toBe(false);
		expect(fresh.clientId).not.toBe(first.clientId);
	});

	it('goes once the server stores it, but text typed while it posted stays, under a fresh id', () => {
		const key = replyDraftKey(scope, 'comment-1');
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'first ', tags);
		const press = keptCommentDrafts.claim(key, 'first ', tags);
		expect(keptCommentDrafts.posted(key, 'first', [])).toBe(true);
		expect(keptCommentDrafts.get(key)).toBeUndefined();

		keptCommentDrafts.keepReply(key, 'first', tags);
		const again = keptCommentDrafts.claim(key, 'first', tags);
		expect(again.clientId).not.toBe(press.clientId);
		keptCommentDrafts.keepReply(key, 'first and more', tags);
		expect(keptCommentDrafts.posted(key, 'first', [])).toBe(false);
		expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'first and more' });
		expect(keptCommentDrafts.get(key)?.clientId).toBeUndefined();
		expect(keptCommentDrafts.claim(key, 'first and more', tags).clientId).not.toBe(again.clientId);
	});

	it('a post stored while its tags changed keeps the new tags, in a copy the field that pressed cannot clear', () => {
		const key = replyDraftKey(scope, 'comment-1');
		const tags = createMentionSelection();
		keptCommentDrafts.keepReply(key, 'Ask @Ada', tags);
		keptCommentDrafts.claim(key, 'Ask @Ada', tags);
		tags.update('Ask @Ada'); tags.select('agent-ada', 4, '@Ada');
		keptCommentDrafts.keepReply(key, 'Ask @Ada', tags);
		expect(keptCommentDrafts.posted(key, 'Ask @Ada', [])).toBe(false);
		// The field that pressed empties its own tags once its post is stored.
		tags.clear();
		const kept = keptCommentDrafts.get(key)!;
		expect(kept.body).toBe('Ask @Ada');
		expect(kept.mentionSelection.occurrences(kept.body).map((item) => item.agent_id)).toEqual(['agent-ada']);
		expect(kept.clientId).toBeUndefined();
	});

	it('typing in one reply re-runs nothing that reads another thread’s reply', () => {
		const typed = replyDraftKey(scope, 'comment-1');
		const kept = replyDraftKey(scope, 'comment-2');
		const empty = replyDraftKey(scope, 'comment-3');
		keptCommentDrafts.keepReply(kept, 'kept', createMentionSelection());
		const runs = { typed: 0, kept: 0, empty: 0 };
		const stop = $effect.root(() => {
			$effect(() => { keptCommentDrafts.get(typed); runs.typed++; });
			$effect(() => { keptCommentDrafts.get(kept); runs.kept++; });
			$effect(() => { keptCommentDrafts.get(empty); runs.empty++; });
		});
		flushSync();
		expect(runs).toEqual({ typed: 1, kept: 1, empty: 1 });
		const tags = createMentionSelection();
		// The first letter, every letter after it, the caret, and emptying the field again.
		for (const text of ['h', 'ha', 'hal', 'half']) { keptCommentDrafts.keepReply(typed, text, tags); flushSync(); }
		keptCommentDrafts.select(typed, [4, 4]); flushSync();
		keptCommentDrafts.keepReply(typed, '', tags); flushSync();
		expect(runs.typed).toBe(7);
		expect(runs.kept).toBe(1);
		expect(runs.empty).toBe(1);
		stop();
	});
});

describe('a box that shows a draft, read once when a post fails', () => {
	/** A box laid out at `rect` (jsdom lays nothing out, so each box reports its own). */
	const box = (rect: DOMRect) => {
		const node = document.createElement('div');
		node.getBoundingClientRect = () => rect;
		return node;
	};
	const inView = () => new DOMRect(20, 100, 300, 80);
	const key = replyDraftKey(scope, 'comment-1');

	it('is on screen only while a box for that draft is mounted and takes room inside the window', () => {
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
		const drop = keptCommentDrafts.shown(key, box(inView()));
		expect(keptCommentDrafts.onScreen(key)).toBe(true);
		// Another draft's box says nothing about this one.
		expect(keptCommentDrafts.onScreen(replyDraftKey(scope, 'comment-2'))).toBe(false);
		drop();
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
	});

	it.each([
		['a hidden or collapsed box (no size)', new DOMRect(20, 100, 0, 0)],
		['a box with height but no width', new DOMRect(20, 100, 0, 80)],
		['a box scrolled above the window', new DOMRect(20, -200, 300, 80)],
		['a box scrolled below the window', new DOMRect(20, window.innerHeight + 10, 300, 80)],
		['a box off to the side', new DOMRect(window.innerWidth + 10, 100, 300, 80)],
	])('is not on screen for %s', (_name, rect) => {
		keptCommentDrafts.shown(key, box(rect));
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
	});

	it('counts any one of several boxes for the same draft', () => {
		keptCommentDrafts.shown(key, box(new DOMRect(20, -200, 300, 80)));
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
		keptCommentDrafts.shown(key, box(inView()));
		expect(keptCommentDrafts.onScreen(key)).toBe(true);
	});

	it('reads the visual viewport, so a box under a phone keyboard is not on screen', () => {
		const original = Object.getOwnPropertyDescriptor(window, 'visualViewport');
		// The keyboard leaves the top 300px; the box sits at 400, inside the layout viewport but under the keyboard.
		Object.defineProperty(window, 'visualViewport', { configurable: true, value: { offsetLeft: 0, offsetTop: 0, width: 393, height: 300 } });
		try {
			keptCommentDrafts.shown(key, box(new DOMRect(10, 400, 300, 80)));
			expect(keptCommentDrafts.onScreen(key)).toBe(false);
			keptCommentDrafts.shown(key, box(new DOMRect(10, 200, 300, 80)));
			expect(keptCommentDrafts.onScreen(key)).toBe(true);
		} finally {
			if (original) Object.defineProperty(window, 'visualViewport', original);
			else delete (window as { visualViewport?: unknown }).visualViewport;
		}
	});

	it('reads the box\'s own scroller, so a box it has scrolled out of its view is not on screen', () => {
		// A sheet or column that scrolls, showing 0-300 of the window; the box sits inside the window but below what it shows.
		const sheet = box(new DOMRect(0, 0, 393, 300));
		sheet.style.overflowY = 'auto';
		const inner = document.createElement('div');
		sheet.append(inner);
		document.body.append(sheet);
		try {
			const hidden = box(new DOMRect(10, 400, 300, 80));
			inner.append(hidden);
			const drop = keptCommentDrafts.shown(key, hidden);
			expect(keptCommentDrafts.onScreen(key)).toBe(false);
			drop();
			// Partly in the part it shows still counts: the person sees the box.
			const showing = box(new DOMRect(10, 260, 300, 80));
			inner.append(showing);
			keptCommentDrafts.shown(key, showing);
			expect(keptCommentDrafts.onScreen(key)).toBe(true);
		} finally {
			sheet.remove();
		}
	});

	it('is registered by `use:showsDraft` while mounted, moves with its key, and is gone on unmount', () => {
		const node = box(inView());
		const action = showsDraft(node, key);
		expect(keptCommentDrafts.onScreen(key)).toBe(true);
		const other = replyDraftKey(scope, 'comment-2');
		action.update(other);
		expect(keptCommentDrafts.onScreen(key)).toBe(false);
		expect(keptCommentDrafts.onScreen(other)).toBe(true);
		action.destroy();
		expect(keptCommentDrafts.onScreen(other)).toBe(false);
	});

	it('registers nothing for a box with no key', () => {
		showsDraft(box(inView()), '');
		expect(keptCommentDrafts.onScreen('')).toBe(false);
	});
});
