import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const mocks = vi.hoisted(() => ({ postComment: vi.fn(), captureComment: vi.fn(), selectComment: vi.fn() }));
vi.mock('$lib/api/docs', async (importOriginal) => ({
	...(await importOriginal<object>()), postComment: mocks.postComment, fetchComments: vi.fn(async () => ({ comments: [] })),
}));
vi.mock('./commentSelection', () => ({ captureComment: mocks.captureComment, selectComment: mocks.selectComment }));

import { docComments } from './docComments';
import { addDocImageComment } from './imageRegionComments';
import type { LiveDocumentSession } from './liveDocumentSession';

const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
const TEXT = '# Notes\n\n![Mock](a.png)\n';
const row = (extra: Record<string, unknown> = {}) => ({
	id: 'c1', anchor_text: '![Mock](a.png)', body: 'Too dark', author: 'you', author_kind: 'user', resolved: false, replies: [],
	created_at: '2026-10-11T00:00:00Z', region_x: 0.1, region_y: 0.2, region_w: 0.3, region_h: 0.1, region_number: 1, image_src: '/ws/a.png', ...extra,
});

afterEach(() => { vi.clearAllMocks(); docComments.set({}); });

describe('a comment on part of an image inside a document', () => {
	it('anchors on the image markdown in the text on screen, with its region, and lists it', async () => {
		mocks.postComment.mockResolvedValueOnce(row());
		const id = await addDocImageComment({ artifactId: 'doc', from: 9, to: 23, text: TEXT }, BOX, 'Too dark');
		expect(id).toBe('c1');
		expect(mocks.postComment).toHaveBeenCalledWith('doc', expect.objectContaining({
			body: 'Too dark', region: BOX, anchor_text: '![Mock](a.png)', from_off: 9, to_off: 23, author: 'you', author_kind: 'user',
		}));
		expect(get(docComments).doc[0]).toMatchObject({ id: 'c1', regionNumber: 1, imageSrc: '/ws/a.png' });
	});

	it('captures the image in a live document as it does a selected quote, and frees the capture when posting fails', async () => {
		const session = { artifactId: 'live-doc', releaseReservation: vi.fn() } as unknown as LiveDocumentSession;
		mocks.selectComment.mockReturnValue('selection');
		mocks.captureComment.mockResolvedValue({ clientId: 'cap-1', quote: '![Mock](a.png)', from: 9, to: 23 });
		mocks.postComment.mockResolvedValueOnce(row({ id: 'live-1' }));
		expect(await addDocImageComment({ artifactId: 'doc', from: 9, to: 23, text: TEXT, session }, BOX, 'Too dark')).toBe('live-1');
		expect(mocks.selectComment).toHaveBeenCalledWith(session, 9, 23);
		expect(mocks.postComment).toHaveBeenCalledWith('live-doc', expect.objectContaining({
			client_id: 'cap-1', region: BOX, anchor_text: '![Mock](a.png)', from_off: 9, to_off: 23, live_capture: true,
		}));

		mocks.postComment.mockRejectedValueOnce(new Error('Offline'));
		await expect(addDocImageComment({ artifactId: 'doc', from: 9, to: 23, text: TEXT, session }, BOX, 'Again')).rejects.toThrow('Offline');
		expect(session.releaseReservation).toHaveBeenCalledWith('cap-1');
	});
});
