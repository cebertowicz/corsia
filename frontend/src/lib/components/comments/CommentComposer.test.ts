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
	it('grows to content and then scrolls at the shared composer limit', async () => {
		const textarea = render();
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
});
