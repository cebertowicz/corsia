// A pressed comment on an image that knows where it goes (`where`) is sent on its own when its press's post does not go
// (`unsentComments`): a failure that passes is not told and the draft keeps its send; a failure that is told stops it.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ postComment: vi.fn(), fetchComments: vi.fn() }));
const toast = vi.hoisted(() => ({ show: vi.fn(() => 'toast'), dismiss: vi.fn() }));
vi.mock('$lib/api/docs', async (original) => ({ ...(await original<object>()), ...api }));
vi.mock('$lib/stores/toast', () => ({ toasts: toast }));

import { docComments } from '$lib/stores/docComments';
import { imageDraftKey, keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';
import { addDocImageComment } from '$lib/stores/imageRegionComments';
import { resetCommentPostTiming } from '$lib/stores/commentPostTiming';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import type { KeptPlace } from '$lib/keptPlace';
import { createImageComments } from './imageComments.svelte';

const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
const rect = () => new DOMRect(10, 10, 40, 20);
const TEXT = '# Notes\n\n![Mock](a.png)\n';
const DOC_IMAGE = imageDraftKey('doc', 'a.png');
const MARKDOWN: KeptPlace = { from: 9, to: 23, quote: '![Mock](a.png)', prefix: '# Notes\n\n', suffix: '\n' };

beforeEach(() => {
	vi.spyOn(console, 'error').mockImplementation(() => {});
	api.fetchComments.mockResolvedValue({ comments: [], feedback: {} });
});
afterEach(() => {
	keptCommentDrafts.reset(); docComments.set({}); resetCommentPostTiming(); vi.clearAllMocks(); vi.restoreAllMocks();
});

const surfaces = [
	['an image artifact', imageDraftKey('image'), (place?: KeptPlace | null) => createImageComments({
		artifactId: () => 'image', readOnly: () => false, restoreFocus: vi.fn(),
		where: () => ({ send: { artifactId: 'image', threadId: null, name: 'flow.png' }, place }),
	}), undefined],
	['an image inside a document', DOC_IMAGE, (place?: KeptPlace | null) => createImageComments({
		artifactId: () => 'doc', draftKey: () => DOC_IMAGE, readOnly: () => false, restoreFocus: vi.fn(),
		post: () => ({ create: (region, text, tags, mentions, press) => addDocImageComment({ artifactId: 'doc', from: 9, to: 23, text: TEXT }, region, text, tags, null, mentions, press) }),
		where: () => ({ send: { artifactId: 'doc', threadId: null, name: 'notes.md' }, place }),
	}), MARKDOWN],
] as const;

for (const [kind, key, make, place] of surfaces) {
	it(`${kind}: a press whose post fails and can be tried again says nothing and keeps its area, sending on its own`, async () => {
		const comments = make(place);
		await comments.draw(BOX, rect);
		api.postComment.mockRejectedValueOnce(new Error('500 boom'));
		await expect(comments.submit('Too dark')).rejects.toThrow('500');
		expect(toast.show).not.toHaveBeenCalled();
		expect(comments.draft?.error).toBeUndefined();
		const kept = keptCommentDrafts.get(key);
		expect(kept).toMatchObject({ body: 'Too dark', region: BOX });
		expect(kept?.error).toBeUndefined();
		expect(kept?.send).toBeDefined();
		expect(unsentComments.sending(key)).toBe(true);
	});

	it(`${kind}: a press the server refuses is told, keeps its area, and does not go on its own`, async () => {
		const comments = make(place);
		await comments.draw(BOX, rect);
		api.postComment.mockRejectedValueOnce(new Error('400 bad'));
		await expect(comments.submit('Too dark')).rejects.toThrow('400');
		expect(keptCommentDrafts.get(key)).toMatchObject({ body: 'Too dark', region: BOX });
		expect(keptCommentDrafts.get(key)?.error).toBeTruthy();
		expect(keptCommentDrafts.get(key)?.send).toBeUndefined();
		expect(unsentComments.sending(key)).toBe(false);
	});
}

it('an image inside a document whose markdown could not be told apart when drawn is told on a failure that passes, and waits', async () => {
	const comments = surfaces[1][2](null);
	await comments.draw(BOX, rect);
	api.postComment.mockRejectedValueOnce(new Error('500 boom'));
	await expect(comments.submit('Too dark')).rejects.toThrow('500');
	expect(keptCommentDrafts.get(DOC_IMAGE)).toMatchObject({ body: 'Too dark', region: BOX });
	expect(keptCommentDrafts.get(DOC_IMAGE)?.error).toBeTruthy();
	expect(unsentComments.sending(DOC_IMAGE)).toBe(false);
});
