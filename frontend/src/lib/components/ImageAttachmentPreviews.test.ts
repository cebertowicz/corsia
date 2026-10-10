import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarComposer from './InputBarComposer.svelte';
import UserBubbleAttachments from './UserBubbleAttachments.svelte';
import type { Attachment } from './inputBarHelpers';
import { token } from '$lib/stores/auth';

let host: HTMLElement;
let components: Record<string, unknown>[] = [];

const image: Attachment = {
	file_id: 'image-1',
	filename: 'photo.heic',
	mime_type: 'image/heic',
	url: '/api/uploads/user-1/image-1/photo.heic',
	is_image: true,
	preview_url: 'blob:preview',
};

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-token');
});

afterEach(() => {
	for (const component of components) unmount(component);
	components = [];
	host.remove();
	token.set(null);
});

function renderComposer() {
	const component = mount(InputBarComposer, {
		target: host,
		props: {
			threadId: 'thread-1',
			pendingAttachments: [image],
			isMultiLine: false,
			sheetView: null,
			toggleSheet: vi.fn(),
			isMobile: false,
			hasImageAttachments: true,
			inputText: '',
			hasMessageToSend: true,
			canSendToModel: true,
			queueUntilSwitch: false,
			onAttachFile: vi.fn(),
			onAttachCamera: vi.fn(),
			onAttachPhotos: vi.fn(),
			onSendClick: vi.fn(),
			onQueueAfterTurn: vi.fn(),
			removeAttachment: vi.fn(),
			handleKeydown: vi.fn(),
			handleInput: vi.fn(),
			handleFocus: vi.fn(),
			handlePaste: vi.fn(),
			onCompositionStart: vi.fn(),
			onCompositionEnd: vi.fn(),
		},
	});
	components.push(component);
	flushSync();
}

describe('image attachment previews', () => {
	it('keeps image semantics in the composer when the browser cannot decode the source', () => {
		renderComposer();
		const preview = host.querySelector<HTMLImageElement>('.attachment-thumb');
		expect(preview).not.toBeNull();

		preview!.dispatchEvent(new Event('error'));
		flushSync();

		expect(host.querySelector('.attachment-thumb')).toBeNull();
		expect(host.querySelector('[aria-label="Image attachment"]')).not.toBeNull();
		expect(host.textContent).toContain('photo.heic');
	});

	it('keeps image semantics in the transcript when the source preview fails', () => {
		const component = mount(UserBubbleAttachments, {
			target: host,
			props: { attachments: [image], onOpenLightbox: vi.fn() },
		});
		components.push(component);
		flushSync();
		const preview = host.querySelector<HTMLImageElement>('.bubble-img');
		expect(preview).not.toBeNull();

		preview!.dispatchEvent(new Event('error'));
		flushSync();

		expect(host.querySelector('.bubble-img')).toBeNull();
		expect(host.querySelector('.bubble-image[aria-label="Image attachment"]')).not.toBeNull();
		expect(host.textContent).toContain('photo.heic');
	});
});
