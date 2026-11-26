// A reply pressed on a document's thread goes on its own when its post does not (`unsentComments`): a failure that passes
// is not told and leaves it going; a failure that is told stops it, as a new comment's does.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
vi.mock('$lib/stores/docComments', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/docComments')>()),
	addReply: vi.fn(),
}));
import { addReply } from '$lib/stores/docComments';
import { createThreadActions } from './threadActions';
import { toasts } from '$lib/stores/toast';
import { documentDraftKey, keptCommentDrafts, replyDraftKey } from '$lib/stores/keptCommentDrafts.svelte';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';

const key = replyDraftKey(documentDraftKey({ projectId: 'project', path: '/documents/doc.md' }), 'root');
beforeEach(() => {
	vi.spyOn(toasts, 'show').mockImplementation(() => 'toast');
	vi.spyOn(console, 'error').mockImplementation(() => {});
	keptCommentDrafts.keepReply(key, 'My reply', createMentionSelection());
});
afterEach(() => { keptCommentDrafts.reset(); unsentComments.reset(); vi.mocked(addReply).mockReset(); vi.restoreAllMocks(); });

it('a failure that passes is not told: no reason, no toast, and the reply keeps going on its own under its press’s id', async () => {
	const offline = new TypeError('Failed to fetch');
	vi.mocked(addReply).mockRejectedValueOnce(offline);
	await expect(createThreadActions(() => 'artifact', vi.fn(), () => 'thread').reply('root', 'My reply', [], [], key)).rejects.toBe(offline);
	expect(toasts.show).not.toHaveBeenCalled();
	const kept = keptCommentDrafts.get(key);
	expect(kept?.error).toBeUndefined();
	expect(kept).toMatchObject({ body: 'My reply', clientId: vi.mocked(addReply).mock.calls[0][7], send: { artifactId: 'artifact', threadId: 'thread' } });
	expect(unsentComments.sending(key)).toBe(true);
});

it('a failure that is told stops it: the reason is kept, it is told, and it no longer goes on its own', async () => {
	const gone = new Error('404 {"detail":"Comment not found"}');
	// While the press is out the reply would go on its own if it failed for a reason that passes.
	let goingDuringPost = false;
	vi.mocked(addReply).mockImplementationOnce(async () => { goingDuringPost = unsentComments.sending(key); throw gone; });
	await expect(createThreadActions(() => 'artifact', vi.fn()).reply('root', 'My reply', [], [], key)).rejects.toBe(gone);
	expect(goingDuringPost).toBe(true);
	expect(toasts.show).toHaveBeenCalledOnce();
	expect(keptCommentDrafts.get(key)?.error).toBeTruthy();
	expect(keptCommentDrafts.get(key)?.send).toBeUndefined();
	expect(unsentComments.sending(key)).toBe(false);
});
