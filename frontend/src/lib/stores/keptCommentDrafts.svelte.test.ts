import { afterEach, describe, expect, it } from 'vitest';
import { flushSync } from 'svelte';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { documentDraftKey, keptCommentDrafts, replyDraftKey } from './keptCommentDrafts.svelte';

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
