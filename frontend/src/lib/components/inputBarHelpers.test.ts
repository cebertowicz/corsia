import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { uploadInputFile, loadUploadLimits, resetUploadLimits, updateInputBarHeight } from './inputBarHelpers';

function fileOfSize(bytes: number, name = 'shot.png', type = 'image/png'): File {
	const file = new File(['x'], name, { type });
	Object.defineProperty(file, 'size', { value: bytes });
	return file;
}

const okUpload = {
	file_id: 'f1',
	filename: 'shot.png',
	mime_type: 'image/png',
	url: '/api/uploads/u/f1/shot.png',
	is_image: true,
};

beforeEach(() => {
	resetUploadLimits();
	// jsdom has no object URLs; attachment previews call this on image uploads.
	Object.defineProperty(URL, 'createObjectURL', {
		value: () => 'blob:preview',
		configurable: true,
	});
});

afterEach(() => {
	vi.unstubAllGlobals();
	resetUploadLimits();
});

describe('upload limits are server-owned', () => {
	it('reads the ceiling from the server instead of a hardcoded 10MB', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).includes('/api/upload/limits')) {
				return new Response(
					JSON.stringify({
						max_bytes: 10 * 1024 * 1024,
						max_image_bytes: 50 * 1024 * 1024,
						max_video_bytes: 500 * 1024 * 1024,
					}),
					{ status: 200 },
				);
			}
			return new Response(JSON.stringify(okUpload), { status: 200 });
		});
		vi.stubGlobal('fetch', fetchMock);
		await loadUploadLimits();
		expect(fetchMock).toHaveBeenCalledWith('/api/upload/limits');

		const { attachment, error } = await uploadInputFile(
			fileOfSize(40 * 1024 * 1024),
			'test-token',
			'thread-1',
		);

		expect(error).toBe('');
		expect(attachment?.file_id).toBe('f1');
	});

	it('rejects an image above the server image ceiling naming that ceiling', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).includes('/api/upload/limits')) {
				return new Response(
					JSON.stringify({
						max_bytes: 10 * 1024 * 1024,
						max_image_bytes: 50 * 1024 * 1024,
						max_video_bytes: 500 * 1024 * 1024,
					}),
					{ status: 200 },
				);
			}
			return new Response(JSON.stringify(okUpload), { status: 200 });
		});
		vi.stubGlobal('fetch', fetchMock);
		await loadUploadLimits();

		const { attachment, error } = await uploadInputFile(
			fileOfSize(51 * 1024 * 1024),
			'test-token',
			'thread-1',
		);

		expect(attachment).toBeNull();
		expect(error).toContain('50MB');
	});

	it('does not carry a 10MB literal in the module source', async () => {
		const source = await import('./inputBarHelpers?raw').then((m) => m.default as string);

		expect(source).not.toContain('10 * 1024 * 1024');
	});
});

describe('composer height is published to its own container (#1949)', () => {
	function composerIn(height: number): { container: HTMLElement; floater: HTMLElement } {
		const container = document.createElement('div');
		const floater = document.createElement('div');
		Object.defineProperty(floater, 'offsetHeight', { configurable: true, get: () => height });
		container.appendChild(floater);
		document.body.appendChild(container);
		return { container, floater };
	}

	it('keeps two mounted composers from overwriting each other or the page', () => {
		const chat = composerIn(140);
		const leaf = composerIn(72);

		updateInputBarHeight(chat.floater);
		updateInputBarHeight(leaf.floater);

		expect(chat.container.style.getPropertyValue('--input-bar-height')).toBe('140px');
		expect(leaf.container.style.getPropertyValue('--input-bar-height')).toBe('72px');
		expect(document.documentElement.style.getPropertyValue('--input-bar-height')).toBe('');
		chat.container.remove();
		leaf.container.remove();
	});
});
