import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentComposer from './CommentComposer.svelte';
import { postDocumentComment, submitDocumentComment } from '$lib/docViewerOperations';
import { toasts } from '$lib/stores/toast';
import { keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';
import { user } from '$lib/stores/auth';
import { createMentionSelection } from './mentionSelection';
import { get } from 'svelte/store';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(onSubmit: (text: string) => Promise<void> = async () => {}, extra: Record<string, unknown> = {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(CommentComposer, {
		target: host,
		props: { quote: 'Selected text', onSubmit, onCancel: () => {}, ...extra },
	});
	flushSync();
	return host.querySelector<HTMLTextAreaElement>('textarea')!;
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

describe('CommentComposer', () => {
	it('autosize stops at whatever max-height the field carries and then scrolls', async () => {
		const textarea = render();
		// jsdom applies no stylesheet, so this proves the autosize cap logic against an inline 150px, not the real text-field.css cap.
		textarea.style.maxHeight = '150px';
		Object.defineProperty(textarea, 'scrollHeight', { configurable: true, value: 240 });
		textarea.value = 'A long comment';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		await vi.waitFor(() => expect(textarea.style.height).toBe('150px'));
		expect(textarea.style.overflowY).toBe('auto');
	});

	it('keeps the mounted draft when its document has no artifact identity', async () => {
		const ensureArtifactId = vi.fn().mockResolvedValue(null);
		const addComment = vi.fn(async () => {});
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'failure-toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			const textarea = render(() => submitDocumentComment(() => postDocumentComment(ensureArtifactId, addComment)));
			textarea.value = 'Keep this draft';
			textarea.dispatchEvent(new Event('input', { bubbles: true }));
			flushSync();
			host.querySelector<HTMLButtonElement>('.submit')!.click();

			await vi.waitFor(() => expect(report).toHaveBeenCalledWith('Couldn’t post the comment. Your draft is kept. Try again.', 'error'));
			expect(textarea.value).toBe('Keep this draft');
			expect(addComment).not.toHaveBeenCalled();
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it('keeps the draft when the save request is refused', async () => {
		const report = vi.spyOn(toasts, 'show').mockImplementation(() => 'failure-toast');
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		try {
			const post = vi.fn().mockRejectedValue(new Error('refused'));
			const textarea = render(() => submitDocumentComment(post));
			textarea.value = 'Keep this refused draft';
			textarea.dispatchEvent(new Event('input', { bubbles: true }));
			flushSync();
			host.querySelector<HTMLButtonElement>('.submit')!.click();

			await vi.waitFor(() => expect(report).toHaveBeenCalledWith('Couldn’t post the comment. Your draft is kept. Try again.', 'error'));
			expect(textarea.value).toBe('Keep this refused draft');
			expect(post).toHaveBeenCalledOnce();
		} finally {
			report.mockRestore();
			log.mockRestore();
		}
	});

	it('clears only after the save acknowledgement resolves', async () => {
		let acknowledge!: () => void;
		const onSubmit = vi.fn(() => new Promise<void>((resolve) => { acknowledge = resolve; }));
		const textarea = render(onSubmit);
		textarea.value = 'Wait for saved acknowledgement';
		textarea.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		host.querySelector<HTMLButtonElement>('.submit')!.click();
		await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith('Wait for saved acknowledgement', [], []));
		expect(textarea.value).toBe('Wait for saved acknowledgement');

		acknowledge();
		await vi.waitFor(() => expect(textarea.value).toBe(''));
	});

	it('offers no Retry, by button or keyboard, for a failure no press can get past', () => {
		const onSubmit = vi.fn(async () => {});
		const textarea = render(onSubmit, { body: 'Kept to copy', error: 'This chat is deleted.', retryable: false });
		expect(host.querySelector('.submit')).toBeNull();
		expect(host.querySelector('.cancel')?.textContent).toBe('Cancel');
		expect(textarea.value).toBe('Kept to copy');
		textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, bubbles: true }));
		expect(onSubmit).not.toHaveBeenCalled();
	});

	it('says why a post failed as an error, and why the button is off as a note', () => {
		// jsdom applies no stylesheet: the colour itself (the error token, as a reply's failure line) is proven in the browser by
		// support/failureLook.ts in comment-failure-one-place and doc-reply-draft-kept.
		render(async () => {}, { body: 'Kept', error: 'Couldn’t post the comment. Your draft is kept. Try again.', disabledReason: 'Select the text again' });
		expect(host.querySelector('[role="alert"]')?.classList.contains('failed')).toBe(true);
		expect(host.querySelector('.selection-reason:not([role="alert"])')?.classList.contains('failed')).toBe(false);
	});

	it('counts as the box that says why a post failed while it is mounted, under the key it is given', () => {
		render(async () => {}, { shownKey: 'draft-key' });
		host.querySelector<HTMLElement>('.composer')!.getBoundingClientRect = () => new DOMRect(20, 100, 300, 160);
		expect(keptCommentDrafts.onScreen('draft-key')).toBe(true);
		unmount(component!); component = null;
		expect(keptCommentDrafts.onScreen('draft-key')).toBe(false);
	});
	it('says under the box, in the same line as a failure, that this device does not keep its draft, until a change is kept', () => {
		user.set({ id: 'person-1', name: 'person-1' });
		try {
			render(async () => {}, { shownKey: 'draft-key', body: 'Kept', error: 'Couldn’t post the comment. Your draft is kept. Try again.' });
			const huge = 'x'.repeat(200_001);
			keptCommentDrafts.keep('draft-key', huge, createMentionSelection(huge));
			flushSync();
			const line = host.querySelector('[role="alert"]');
			expect(line?.textContent).toBe('Couldn’t post the comment. Try again. Too long to keep on this device. Post it or copy it before you close CORSIA.');
			expect(line?.classList.contains('failed')).toBe(true);
			keptCommentDrafts.keep('draft-key', 'Kept', createMentionSelection('Kept'));
			flushSync();
			expect(host.querySelector('[role="alert"]')?.textContent).toBe('Couldn’t post the comment. Your draft is kept. Try again.');
		} finally {
			for (const toast of get(toasts)) toasts.dismiss(toast.id);
			keptCommentDrafts.reset(); user.set(null); localStorage.clear();
		}
	});
});
