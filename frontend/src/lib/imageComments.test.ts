import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const mocks = vi.hoisted(() => ({ addRegionComment: vi.fn(), moveRegion: vi.fn(), show: vi.fn(() => 'toast'), dismiss: vi.fn() }));
vi.mock('$lib/stores/imageRegionComments', () => ({ addRegionComment: mocks.addRegionComment, moveRegion: mocks.moveRegion }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.show, dismiss: mocks.dismiss } }));

import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { DocRequestError } from '$lib/api/docs';
import { createImageComments, regionLabel } from './imageComments.svelte';

const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
const rect = () => new DOMRect(10, 10, 40, 20);
function controller(readOnly = false) {
	const restoreFocus = vi.fn(), reveal = vi.fn(), onCreated = vi.fn();
	const comments = createImageComments({ artifactId: () => 'image', readOnly: () => readOnly, restoreFocus, reveal, onCreated });
	return { comments, restoreFocus, reveal, onCreated };
}
const escape = () => new KeyboardEvent('keydown', { key: 'Escape', cancelable: true });

afterEach(() => { vi.clearAllMocks(); focusedDocComment.set(null); });

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
		expect(comments.draft).toEqual({ text: '', label: 'Box on the image', body: '' });
		expect(comments.cardOpen).toBe(true);
	});

	it('posts the comment on its region and opens its card where it was drawn', async () => {
		mocks.addRegionComment.mockResolvedValueOnce('new');
		const { comments, onCreated } = controller();
		await comments.draw(BOX, rect);
		comments.setDraftBody('Too dark');
		expect(comments.hasDraftText).toBe(true);
		await comments.submit('Too dark');
		expect(mocks.addRegionComment).toHaveBeenCalledWith('image', BOX, 'Too dark');
		expect(onCreated).toHaveBeenCalledOnce();
		expect(comments.ui.state).toEqual({ kind: 'thread', id: 'new' });
		expect(comments.selectedId).toBe('new');
		expect(comments.draft).toBeNull();
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
			if (source === 'old') throw new DocRequestError('409 {"detail":"reworded","kind":"diagram_changed"}', 409, 'diagram_changed');
			return 'new';
		});
		const refreshDiagram = vi.fn(async () => { source = 'new'; return true; });
		const comments = createImageComments({ artifactId: () => 'doc', readOnly: () => false,
			restoreFocus: () => {}, post: () => ({ create: post }), refreshDiagram });
		await comments.draw(BOX, rect);
		comments.setDraftBody('Keep this');
		await expect(comments.submit('Keep this')).rejects.toThrow('409');
		expect(refreshDiagram).toHaveBeenCalledOnce();
		expect(comments.boxMode).toBe(true);
		await comments.draw({ x: .6, y: .6, w: 0, h: 0 }, rect);
		expect(comments.draft?.body).toBe('Keep this');
		await comments.submit('Keep this');
		expect(post).toHaveBeenLastCalledWith({ x: .6, y: .6, w: 0, h: 0 }, 'Keep this');
		expect(comments.selectedId).toBe('new');
	});

	it.each([
		['the server refuses through the document request', undefined],
		['the live document words the failure itself', () => 'The document changed before your comment could be attached. Your draft is kept. Try again.'],
	])('says to place the pin on the current diagram, with the draft kept, when %s', async (_flow, failure) => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const post = vi.fn(async () => { throw new DocRequestError('409 {"kind":"diagram_changed"}', 409, 'diagram_changed'); });
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
		const post = vi.fn(async () => { throw new DocRequestError('409 {"kind":"selection_changed"}', 409, 'selection_changed'); });
		const comments = createImageComments({ artifactId: () => 'doc', readOnly: () => false,
			restoreFocus: () => {}, post: () => ({ create: post, failure: () => 'Live words' }), refreshDiagram: async () => true });
		await comments.draw(BOX, rect);
		await expect(comments.submit('Keep this')).rejects.toThrow('409');
		expect(mocks.show).toHaveBeenCalledWith('Live words', 'error');
	});

	it('refreshes on the refusal kind, never on its wording', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const post = vi.fn(async () => { throw new DocRequestError('409 {"detail":"This diagram changed."}', 409, 'selection_changed'); });
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

	it('keeps the drawer open when a tag is picked, and selects that comment there', () => {
		const { comments } = controller();
		comments.ui.openList(document.createElement('button'));
		comments.pick('c3', () => null, document.createElement('button'));
		expect(comments.ui.state).toEqual({ kind: 'list' });
		expect(comments.selectedId).toBe('c3');
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
