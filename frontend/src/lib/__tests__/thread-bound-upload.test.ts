import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { resetUploadLimits, uploadThreadAttachment } from '$lib/components/inputBarHelpers';

const file = new File(['thread upload'], 'report.txt', { type: 'text/plain' });

beforeEach(() => resetUploadLimits());

afterEach(() => {
	vi.unstubAllGlobals();
	resetUploadLimits();
});

describe('thread-bound composer uploads', () => {
	it('creates a durable thread before sending the upload request', async () => {
		let threadId: string | null = null;
		const ensureThread = vi.fn(async () => {
			threadId = 'new-thread';
		});
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).includes('/api/upload/limits')) {
				return new Response(JSON.stringify({
					max_bytes: 10 * 1024 * 1024,
					max_image_bytes: 50 * 1024 * 1024,
					max_video_bytes: 500 * 1024 * 1024,
				}), { status: 200 });
			}
			return new Response(JSON.stringify({
				file_id: 'file-1',
				filename: 'report.txt',
				mime_type: 'text/plain',
				url: '/api/uploads/steve/file-1/report.txt',
				is_image: false,
			}), { status: 200 });
		});
		vi.stubGlobal('fetch', fetchMock);

		const result = await uploadThreadAttachment(
			file,
			'auth',
			() => threadId,
			ensureThread,
		);

		expect(result.error).toBe('');
		expect(ensureThread).toHaveBeenCalledOnce();
		const uploadCalls = fetchMock.mock.calls.filter(([input]) =>
			String(input).startsWith('/api/upload?'),
		);
		expect(uploadCalls).toHaveLength(1);
		expect(String(uploadCalls[0][0])).toContain('thread_id=new-thread');
	});

	it('does not upload when session creation leaves no durable thread', async () => {
		const ensureThread = vi.fn(async () => {});
		const fetchMock = vi.fn();
		vi.stubGlobal('fetch', fetchMock);

		const result = await uploadThreadAttachment(
			file,
			'auth',
			() => null,
			ensureThread,
		);

		expect(result).toEqual({
			attachment: null,
			error: 'Could not create a conversation for this attachment',
		});
		expect(fetchMock).not.toHaveBeenCalled();
	});
});
