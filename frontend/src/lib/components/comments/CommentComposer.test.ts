import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommentComposer from './CommentComposer.svelte';
import { postDocumentComment, submitDocumentComment } from '$lib/docViewerOperations';
import { toasts } from '$lib/stores/toast';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(onSubmit: (text: string) => Promise<void> = async () => {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(CommentComposer, {
		target: host,
		props: { quote: 'Selected text', onSubmit, onCancel: () => {} },
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

			await vi.waitFor(() => expect(report).toHaveBeenCalledWith("Couldn't post the comment — try again", 'error'));
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

			await vi.waitFor(() => expect(report).toHaveBeenCalledWith("Couldn't post the comment — try again", 'error'));
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
		await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith('Wait for saved acknowledgement'));
		expect(textarea.value).toBe('Wait for saved acknowledgement');

		acknowledge();
		await vi.waitFor(() => expect(textarea.value).toBe(''));
	});
});
