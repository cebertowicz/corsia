import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const mocks = vi.hoisted(() => ({ addRegionComment: vi.fn(), moveRegion: vi.fn(), show: vi.fn(() => 'toast'), dismiss: vi.fn() }));
vi.mock('$lib/stores/imageRegionComments', () => ({ addRegionComment: mocks.addRegionComment, moveRegion: mocks.moveRegion }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.show, dismiss: mocks.dismiss } }));

import { focusedDocComment } from '$lib/stores/docCommentFocus';
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

	it('keeps the newly drawn pin visible while its move is saving, then shows its card', async () => {
		let settle: () => void = () => {};
		mocks.moveRegion.mockImplementationOnce(() => new Promise<void>((resolve) => { settle = resolve; }));
		const { comments } = controller();
		comments.movePin('pin');
		expect(comments.boxMode).toBe(true);
		const move = comments.draw({ x: 0.5, y: 0.5, w: 0, h: 0 }, rect);
		expect(mocks.moveRegion).toHaveBeenCalledWith('image', 'pin', { x: 0.5, y: 0.5, w: 0, h: 0 });
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
		expect(reveal).toHaveBeenCalledWith(BOX);
	});

	it('keeps the drawer open when a tag is picked, and selects that comment there', () => {
		const { comments } = controller();
		comments.ui.openList(document.createElement('button'));
		comments.pick('c3', () => null, document.createElement('button'));
		expect(comments.ui.state).toEqual({ kind: 'list' });
		expect(comments.selectedId).toBe('c3');
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
