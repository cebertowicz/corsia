import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const mocks = vi.hoisted(() => ({ addRegionComment: vi.fn(), moveRegion: vi.fn(), show: vi.fn(() => 'toast'), dismiss: vi.fn() }));
vi.mock('$lib/stores/imageRegionComments', () => ({ addRegionComment: mocks.addRegionComment, moveRegion: mocks.moveRegion }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.show, dismiss: mocks.dismiss } }));

import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { pickedComments } from '$lib/stores/docComments';
import { RequestRefused } from '$lib/api/docs';
import { createImageComments, regionLabel } from './imageComments.svelte';
import { keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';

const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
const rect = () => new DOMRect(10, 10, 40, 20);
function controller(readOnly = false) {
	const restoreFocus = vi.fn(), reveal = vi.fn(), onCreated = vi.fn();
	const comments = createImageComments({ artifactId: () => 'image', readOnly: () => readOnly, restoreFocus, reveal, onCreated });
	return { comments, restoreFocus, reveal, onCreated };
}
const escape = () => new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });

afterEach(() => { vi.clearAllMocks(); focusedDocComment.set(null); pickedComments.set(new Set()); keptCommentDrafts.reset(); });

describe('image comments', () => {
	it('names what a new comment is on', () => {
		expect(regionLabel(BOX)).toBe('Box on the image');
		expect(regionLabel({ x: 0.5, y: 0.5, w: 0, h: 0 })).toBe('Point on the image');
		expect(regionLabel({ x: 0, y: 0, w: 1, h: 1 })).toBe('The whole image');
	});

	it('opens the composer for a drawn box and ends box mode after one', async () => {
		const { comments } = controller();
		comments.toggleBoxMode();
		expect(comments.boxMode).toBe(true);
		await comments.draw(BOX, rect);
		expect(comments.boxMode).toBe(false);
		expect(comments.ui.state).toEqual({ kind: 'draft' });
		expect(comments.draft).toEqual(expect.objectContaining({ text: '', label: 'Box on the image', body: '' }));
		expect(comments.cardOpen).toBe(true);
	});

	it('posts the comment on its region and opens its card where it was drawn', async () => {
		mocks.addRegionComment.mockResolvedValueOnce('new');
		const { comments, onCreated } = controller();
		await comments.draw(BOX, rect);
		comments.setDraftBody('Too dark');
		expect(comments.hasDraftText).toBe(true);
		await comments.submit('Too dark');
		expect(mocks.addRegionComment).toHaveBeenCalledWith('image', BOX, 'Too dark', [], null, [], { clientId: expect.any(String), again: false });
		expect(onCreated).toHaveBeenCalledOnce();
		expect(comments.ui.state).toEqual({ kind: 'thread', id: 'new' });
		expect(comments.selectedId).toBe('new');
		expect(comments.draft).toBeNull();
	});

	it('passes selected tags and viewing thread for an image comment', async () => {
		mocks.addRegionComment.mockResolvedValueOnce('new');
		const comments = createImageComments({ artifactId: () => 'image', threadId: () => 'viewer', readOnly: () => false, restoreFocus: vi.fn() });
		await comments.draw(BOX, rect);
		const mentions = [{ agent_id: 'agent', start: 0, end: 6 }];
		await comments.submit('@Agent', ['agent'], mentions);
		expect(mocks.addRegionComment).toHaveBeenCalledWith('image', BOX, '@Agent', ['agent'], 'viewer', mentions, { clientId: expect.any(String), again: false });
	});

	it('keeps a tagged draft and explains a missing viewing thread', async () => {
		mocks.addRegionComment.mockRejectedValueOnce(new RequestRefused('400 {"detail":"refused","kind":"no_viewing_thread"}', 'no_viewing_thread'));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const { comments } = controller();
		await comments.draw(BOX, rect);
		comments.setDraftBody('@Agent');
		await expect(comments.submit('@Agent', ['agent'])).rejects.toThrow();
		expect(mocks.show).toHaveBeenCalledWith('Open this document from a chat to tag agents. Your comment is still here.', 'error');
		expect(comments.draft?.body).toBe('@Agent');
	});

	it('names a tag that can no longer be used by the refusal kind, not by its English text', async () => {
		mocks.addRegionComment.mockRejectedValueOnce(new RequestRefused('400 {"detail":"refused","kind":"tag_unavailable"}', 'tag_unavailable'));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const { comments } = controller();
		await comments.draw(BOX, rect);
		comments.setDraftBody('@Agent');
		await expect(comments.submit('@Agent', ['agent'])).rejects.toThrow();
		expect(mocks.show).toHaveBeenCalledWith('An agent you tagged can’t be tagged from this chat. Remove its tag and post again. Your comment is still here.', 'error');
		expect(comments.draft?.body).toBe('@Agent');
	});

	it('keeps the draft and shows the server\'s sentence, without "try again", when the document no longer takes comments', async () => {
		const sentence = 'Comments can’t be added to this document. Your comment wasn’t sent, and your text is still here.';
		mocks.addRegionComment.mockRejectedValueOnce(new RequestRefused(`409 ${JSON.stringify({ detail: sentence, kind: 'not_commentable' })}`, 'not_commentable'));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const { comments, onCreated } = controller();
		await comments.draw(BOX, rect);
		comments.setDraftBody('Too dark');
		await expect(comments.submit('Too dark')).rejects.toThrow('409');
		expect(mocks.show).toHaveBeenCalledWith(sentence, 'error');
		expect(mocks.addRegionComment).toHaveBeenCalledOnce();
		expect(comments.draft?.body).toBe('Too dark');
		expect(comments.ui.state).toEqual({ kind: 'draft' });
		expect(onCreated).not.toHaveBeenCalled();
	});

	it('keeps the composer and says why when posting fails', async () => {
		mocks.addRegionComment.mockRejectedValueOnce(new Error('Offline'));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const { comments } = controller();
		await comments.draw(BOX, rect);
		await expect(comments.submit('Too dark')).rejects.toThrow('Offline');
		expect(mocks.show).toHaveBeenCalledWith("Couldn't post the comment — try again", 'error');
		expect(comments.ui.state).toEqual({ kind: 'draft' });
		expect(comments.draftRegion).toEqual(BOX);
	});

	it('refreshes a changed diagram and keeps the draft for a new placement', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		let source = 'old';
		const post = vi.fn(async (_region, _text) => {
			if (source === 'old') throw new RequestRefused('409 {"detail":"reworded","kind":"diagram_changed"}', 'diagram_changed');
			return 'new';
		});
		const refreshDiagram = vi.fn(async () => { source = 'new'; return true; });
		const comments = createImageComments({ artifactId: () => 'doc', readOnly: () => false,
			restoreFocus: () => {}, post: () => ({ create: post }), refreshDiagram });
		await comments.draw(BOX, rect);
		const clearTags = vi.spyOn(comments.draft!.mentionSelection, 'clear');
		comments.setDraftBody('Keep this');
		await expect(comments.submit('Keep this')).rejects.toThrow('409');
		expect(refreshDiagram).toHaveBeenCalledOnce();
		expect(comments.boxMode).toBe(true);
		await comments.draw({ x: .6, y: .6, w: 0, h: 0 }, rect);
		expect(comments.draft?.body).toBe('Keep this');
		// The kept draft keeps its picked tags with its text.
		expect(clearTags).not.toHaveBeenCalled();
		await comments.submit('Keep this');
		expect(post).toHaveBeenLastCalledWith({ x: .6, y: .6, w: 0, h: 0 }, 'Keep this', [], [], expect.objectContaining({ clientId: expect.any(String) }));
		expect(comments.selectedId).toBe('new');
	});

	it.each([
		['the server refuses through the document request', undefined],
		['the live document words the failure itself', () => 'The document changed before your comment could be attached. Your draft is kept. Try again.'],
	])('says to place the pin on the current diagram, with the draft kept, when %s', async (_flow, failure) => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const post = vi.fn(async () => { throw new RequestRefused('409 {"kind":"diagram_changed"}', 'diagram_changed'); });
		const comments = createImageComments({ artifactId: () => 'doc', readOnly: () => false,
			restoreFocus: () => {}, post: () => ({ create: post, failure }), refreshDiagram: async () => true });
		await comments.draw(BOX, rect);
		comments.setDraftBody('Keep this');
		await expect(comments.submit('Keep this')).rejects.toThrow('409');
		expect(mocks.show).toHaveBeenCalledWith('Place the pin on the current diagram; your draft is kept.', 'error');
		expect(comments.boxMode).toBe(true);
		expect(comments.hasDraftText).toBe(true);
	});

	it('keeps the failure wording of any other refusal', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const post = vi.fn(async () => { throw new RequestRefused('409 {"kind":"selection_changed"}', 'selection_changed'); });
		const comments = createImageComments({ artifactId: () => 'doc', readOnly: () => false,
			restoreFocus: () => {}, post: () => ({ create: post, failure: () => 'Live words' }), refreshDiagram: async () => true });
		await comments.draw(BOX, rect);
		await expect(comments.submit('Keep this')).rejects.toThrow('409');
		expect(mocks.show).toHaveBeenCalledWith('Live words', 'error');
	});

	it('refreshes on the refusal kind, never on its wording', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const post = vi.fn(async () => { throw new RequestRefused('409 {"detail":"This diagram changed."}', 'selection_changed'); });
		const refreshDiagram = vi.fn(async () => true);
		const comments = createImageComments({ artifactId: () => 'doc', readOnly: () => false,
			restoreFocus: () => {}, post: () => ({ create: post }), refreshDiagram });
		await comments.draw(BOX, rect);
		await expect(comments.submit('Keep this')).rejects.toThrow('409');
		expect(refreshDiagram).not.toHaveBeenCalled();
	});

	it('keeps the newly drawn pin visible while its move is saving, then shows its card', async () => {
		let settle: () => void = () => {};
		mocks.moveRegion.mockImplementationOnce(() => new Promise<void>((resolve) => { settle = resolve; }));
		const { comments } = controller();
		comments.movePin('pin');
		expect(comments.boxMode).toBe(true);
		const move = comments.draw({ x: 0.5, y: 0.5, w: 0, h: 0 }, rect);
		expect(mocks.moveRegion).toHaveBeenCalledWith('image', 'pin', { x: 0.5, y: 0.5, w: 0, h: 0 }, undefined);
		expect(comments.moving).toBe('pin');
		expect(comments.movingRegion).toEqual({ x: 0.5, y: 0.5, w: 0, h: 0 });
		expect(comments.moveSaving).toBe(true);
		settle();
		await move;
		expect(mocks.addRegionComment).not.toHaveBeenCalled();
		expect(comments.moving).toBeNull();
		expect(comments.movingRegion).toBeNull();
		expect(comments.selectedId).toBe('pin');
	});

	it('closes a card first, then leaves box mode, then lets Escape reach Back', async () => {
		const { comments, restoreFocus } = controller();
		comments.pick('c1', rect, document.createElement('button'));
		comments.toggleBoxMode();
		let event = escape();
		comments.onKeydownCapture(event);
		expect(event.defaultPrevented).toBe(true);
		expect(comments.ui.state).toBeNull();
		expect(restoreFocus).not.toHaveBeenCalled(); // focus goes back to the tag that opened the card
		expect(comments.boxMode).toBe(true);
		event = escape();
		comments.onKeydownCapture(event);
		expect(event.defaultPrevented).toBe(true);
		expect(comments.boxMode).toBe(false);
		event = escape();
		comments.onKeydownCapture(event);
		expect(event.defaultPrevented).toBe(false);
	});

	it('keeps the drawer open when one of its cards is shown on the image', () => {
		const { comments, reveal } = controller();
		comments.ui.openList(document.createElement('button'));
		comments.showFromList('c2', BOX);
		expect(comments.ui.state).toEqual({ kind: 'list' });
		expect(get(focusedDocComment)).toBe('c2');
		expect(comments.selectedId).toBe('c2');
		expect(reveal).toHaveBeenCalledWith('c2', BOX, true);
	});

	it('marks ambiguous candidates in the existing drawer and clears them when a card is selected', () => {
		const { comments } = controller();
		const opener = document.createElement('button');
		comments.pickCandidates(['c3', 'c4'], opener);
		expect(comments.ui.state).toEqual({ kind: 'list' });
		expect(comments.listCandidates).toEqual(['c3', 'c4']);
		expect(comments.selectedId).toBe('c3');
		comments.pickCandidates(['c4', 'c5'], opener);
		expect(comments.ui.state).toEqual({ kind: 'list' });
		expect(comments.listCandidates).toEqual(['c4', 'c5']);
		comments.showFromList('c5', BOX);
		expect(comments.listCandidates).toBeNull();
		expect(comments.listCandidateOrder).toEqual(['c4', 'c5']);
		expect(comments.selectedId).toBe('c5');
		comments.close();
		comments.openList(opener);
		expect(comments.listCandidateOrder).toBeNull();
	});

	it('keeps the drawer open when a tag is picked, and selects that comment there as a pick that reads it', () => {
		const { comments } = controller();
		comments.ui.openList(document.createElement('button'));
		comments.pick('c3', () => null, document.createElement('button'));
		expect(comments.ui.state).toEqual({ kind: 'list' });
		expect(comments.selectedId).toBe('c3');
		expect([...get(pickedComments)]).toEqual(['c3']);
	});

	it('opens a card the viewer was opened on beside the box it was given, not beside its tag', async () => {
		const { comments, reveal } = controller();
		const tag = document.createElement('button');
		const box = () => new DOMRect(100, 100, 200, 120);
		comments.select('c4', tag, BOX, box);
		await vi.waitFor(() => expect(reveal).toHaveBeenCalledWith('c4', BOX));
		expect(comments.selectedId).toBe('c4');
		expect(comments.ui.anchor).toBe(box);
		// With no box to give, the tag is the anchor, as before.
		comments.select('c5', tag, BOX);
		expect(comments.ui.anchor).not.toBe(box);
		expect(comments.ui.anchor).toBeTypeOf('function');
	});

	it('draws and moves nothing for a reader who may not comment', async () => {
		const { comments } = controller(true);
		comments.toggleBoxMode();
		comments.movePin('pin');
		await comments.draw(BOX, rect);
		expect(comments.boxMode).toBe(false);
		expect(comments.ui.state).toBeNull();
		expect(mocks.moveRegion).not.toHaveBeenCalled();
	});
});

// A person's typed comment on an image is never lost: closing its composer any way but Cancel keeps the text and tags for
// the next composer drawn on that image, even from a new viewer of it.
describe('a typed image comment is never lost', () => {
	type Comments = ReturnType<typeof createImageComments>;
	const tagged = async (comments: Comments) => {
		await comments.draw(BOX, rect);
		comments.setDraftBody('Hey @Agent\nlook');
		// As the mention picker does: it reads the field's text, then records the agent picked in it.
		comments.draft!.mentionSelection.update('Hey @Agent\nlook');
		comments.draft!.mentionSelection.select('agent-1', 4, '@Agent');
	};
	const kept = (comments: Comments) => ({ body: comments.draft?.body, tags: comments.draft?.mentionSelection.occurrences(comments.draft.body) });
	const KEPT = { body: 'Hey @Agent\nlook', tags: [{ agent_id: 'agent-1', start: 4, end: 10 }] };
	it.each([
		['Escape', (comments: Comments) => comments.onKeydownCapture(escape())],
		['a close (the card close, a click outside)', (comments: Comments) => comments.close()],
		['a tag picked while writing', (comments: Comments) => comments.pick('c1', rect, document.createElement('button'))],
		['tags picked in the drawer while writing', (comments: Comments) => comments.pickCandidates(['c1', 'c2'], document.createElement('button'))],
		['a card opened from the document while writing', (comments: Comments) => comments.select('c1', document.createElement('button'), BOX)],
	])('path 15/16: %s keeps the text and its tags for the next box drawn', async (_name, leave) => {
		const { comments } = controller();
		await tagged(comments);
		leave(comments);
		comments.close();
		expect(comments.draft).toBeNull();
		await comments.draw({ x: 0.5, y: 0.5, w: 0, h: 0 }, rect);
		expect(kept(comments)).toEqual(KEPT);
		expect(comments.hasDraftText).toBe(true);
		comments.cancel();
	});
	// Path 15, Cancel as the one discard, is driven through the button the person presses in ImageCommentLightbox.svelte.test.ts.
	it('a new viewer of the same image keeps the text; another image does not take it', async () => {
		const first = controller().comments;
		await tagged(first);
		first.close();
		const other = createImageComments({ artifactId: () => 'image', draftKey: () => 'image|other.png', readOnly: () => false, restoreFocus: vi.fn() });
		await other.draw(BOX, rect);
		expect(other.draft?.body).toBe('');
		const again = controller().comments;
		await again.draw(BOX, rect);
		expect(kept(again)).toEqual(KEPT);
		again.cancel();
	});
	it('a posted comment does not come back in the next composer', async () => {
		mocks.addRegionComment.mockResolvedValueOnce('new');
		const { comments } = controller();
		await tagged(comments);
		await comments.submit('Hey @Agent\nlook');
		comments.close();
		await comments.draw(BOX, rect);
		expect(kept(comments)).toEqual({ body: '', tags: [] });
	});
});
