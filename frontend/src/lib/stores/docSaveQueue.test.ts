import { afterEach, describe, expect, it, vi } from 'vitest';
import { createHash } from 'node:crypto';
import { get } from 'svelte/store';

const sha = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');
const expectedOf = (call: unknown[]) => (call[2] as { expectedSha256?: string | null }).expectedSha256;
const staleRefusal = () => new Error(`409 ${JSON.stringify({ detail: { code: 'stale_base', message: 'This file changed after you started editing.' } })}`);

const mocks = vi.hoisted(() => ({
	writeContent: vi.fn(),
	flushPositions: vi.fn(),
	toastShow: vi.fn(),
}));

vi.mock('$lib/api/docs', () => ({ writeContent: mocks.writeContent }));
vi.mock('$lib/stores/docComments', () => ({ flushPositions: mocks.flushPositions }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.toastShow } }));

import {
	clearDraft, closeTabWithSaveGate, docSaveActivities, docSaveDrafts, draftKey, withDraftArtifactId,
	enrichDraftTarget, existingDraft, flushDraft, relocateDraftTarget, seedDraft, setDraftText,
} from './docSaveQueue';
import type { DocViewerTarget } from '$lib/docViewerTarget';

let instanceSeq = 0;

function target(overrides: Partial<DocViewerTarget> = {}): DocViewerTarget {
	return {
		instanceId: `tab-${++instanceSeq}`,
		path: '/project/note.md',
		name: 'note.md',
		projectId: 'project-a',
		threadId: 'thread-a',
		artifactId: 'artifact-a',
		...overrides,
	};
}

function deferred<T = unknown>() {
	let resolve!: (value: T | PromiseLike<T>) => void;
	let reject!: (reason?: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

afterEach(() => {
	vi.restoreAllMocks();
	mocks.writeContent.mockReset();
	mocks.flushPositions.mockReset();
	mocks.toastShow.mockReset();
});

describe('docSaveQueue', () => {
	it('publishes the in-flight transition after a slow write settles', async () => {
		const doc = target({ path: '/project/activity.md' });
		const slow = deferred();
		mocks.writeContent.mockReturnValueOnce(slow.promise);
		seedDraft(doc, 'base');
		setDraftText(doc, 'changed');
		const saving = flushDraft(doc);
		await vi.waitFor(() => expect(get(docSaveActivities)[draftKey(doc)]?.writeInFlight).toBe(true));
		slow.resolve({});
		await saving;
		expect(get(docSaveActivities)[draftKey(doc)]).toMatchObject({ writeInFlight: false, resaveQueued: false });
		clearDraft(doc);
	});

	it('serializes delayed writes so stale text cannot complete after newer text', async () => {
		const doc = target({ path: '/project/ordered.md', artifactId: 'ordered' });
		const first = deferred();
		const second = deferred();
		mocks.writeContent
			.mockImplementationOnce(() => first.promise)
			.mockImplementationOnce(() => second.promise);
		seedDraft(doc, 'base');

		setDraftText(doc, 'older');
		const olderFlush = flushDraft(doc);
		await vi.waitFor(() => expect(mocks.writeContent).toHaveBeenCalledTimes(1));

		setDraftText(doc, 'newer');
		const newerFlush = flushDraft(doc);
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);

		first.resolve({});
		await vi.waitFor(() => expect(mocks.writeContent).toHaveBeenCalledTimes(2));
		expect(mocks.writeContent.mock.calls.map(([, text]) => text)).toEqual(['older', 'newer']);

		second.resolve({});
		await Promise.all([olderFlush, newerFlush]);
		expect(existingDraft(doc)).toMatchObject({ editText: 'newer', savedText: 'newer', status: 'saved' });
		clearDraft(doc);
	});

	it('keeps drafts independent per tab instance, including the same doc in two threads', () => {
		const here = target({ instanceId: 'tab-here' });
		const sameArtifactOtherThread = target({ instanceId: 'tab-there', threadId: 'thread-b' });

		expect(draftKey(here)).toBe('tab-here');
		expect(draftKey(here)).not.toBe(draftKey(sameArtifactOtherThread));
	});

	it('keeps one tab’s draft addressable after its identity is enriched or it relocates', () => {
		const doc = target({ instanceId: 'tab-stable', artifactId: null, path: '/project/moving.md' });
		seedDraft(doc, 'server text');
		setDraftText(doc, 'local edit');

		enrichDraftTarget(doc, 'resolved-artifact');
		relocateDraftTarget(doc, '/archive/moving.md');

		// Same key, same unsaved text — nothing was orphaned by either change.
		expect(draftKey(doc)).toBe('tab-stable');
		expect(existingDraft(doc)).toMatchObject({
			editText: 'local edit',
			target: { artifactId: 'resolved-artifact', path: '/archive/moving.md' },
		});
		clearDraft(doc);
	});

	it('records the revision the server reported for its own save', async () => {
		const doc = target({ instanceId: 'tab-rev', path: '/project/rev.md' });
		mocks.writeContent.mockResolvedValue({ saved: true, revision: 'r-99' });
		seedDraft(doc, 'base', 'r-1');
		setDraftText(doc, 'changed');
		await flushDraft(doc);
		expect(existingDraft(doc)?.savedRevision).toBe('r-99');
		expect(mocks.toastShow).not.toHaveBeenCalled();
		clearDraft(doc);
	});

	it('keeps a save as saved but shows the failed context notice once', async () => {
		const doc = target({ instanceId: 'tab-notice', path: '/project/context/PROJECT.md' });
		const notice = 'File saved. The context-change notice could not be recorded; other agents may still use earlier instructions.';
		mocks.writeContent.mockResolvedValue({ saved: true, revision: 'r-2', context_notice_error: notice });
		seedDraft(doc, 'base', 'r-1');
		setDraftText(doc, 'changed');

		expect(await flushDraft(doc)).toBe(true);
		expect(existingDraft(doc)).toMatchObject({
			editText: 'changed', savedText: 'changed', status: 'saved', error: '', savedRevision: 'r-2',
		});
		expect(mocks.toastShow).toHaveBeenCalledOnce();
		expect(mocks.toastShow).toHaveBeenCalledWith(notice, 'error', expect.any(Number));
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);
		clearDraft(doc);
	});

	it('flushes mapped comment positions against the saved draft text', async () => {
		const doc = target({ artifactId: 'anchored-comment', path: '/project/comment.md' });
		mocks.writeContent.mockResolvedValue({ saved: true });
		seedDraft(doc, 'before quote');
		setDraftText(doc, 'inserted before quote');
		await flushDraft(doc);
		expect(mocks.flushPositions).toHaveBeenCalledWith('anchored-comment', 'inserted before quote');
		clearDraft(doc);
	});

	it('preserves a path-keyed draft when the server resolves an artifact id', async () => {
		const pathOnly = target({ artifactId: null, path: '/project/unregistered.md' });
		mocks.writeContent.mockResolvedValue({});
		seedDraft(pathOnly, 'server text');
		enrichDraftTarget(pathOnly, 'resolved-artifact');

		expect(existingDraft(pathOnly)).toMatchObject({
			target: { artifactId: 'resolved-artifact' },
			editText: 'server text',
			savedText: 'server text',
		});
		expect(get(docSaveDrafts)).toHaveProperty(draftKey(pathOnly));

		setDraftText(pathOnly, 'local revision');
		expect(withDraftArtifactId(pathOnly)).toMatchObject({
			artifactId: 'resolved-artifact',
			path: '/project/unregistered.md',
		});
		await flushDraft(pathOnly);
		expect(existingDraft(pathOnly)).toMatchObject({
			target: { artifactId: 'resolved-artifact' },
			editText: 'local revision',
			savedText: 'local revision',
		});
		clearDraft(pathOnly);
	});

	it('awaits an already in-flight inactive-tab save before refusing its failed close', async () => {
		const doc = target({ path: '/project/inflight-failing.md', artifactId: 'inflight-failing' });
		const first = deferred();
		mocks.writeContent.mockImplementationOnce(() => first.promise);
		vi.spyOn(console, 'error').mockImplementation(() => {});
		seedDraft(doc, 'server text');
		setDraftText(doc, 'recoverable local text');

		const saving = flushDraft(doc);
		await vi.waitFor(() => expect(mocks.writeContent).toHaveBeenCalledTimes(1));

		const onClose = vi.fn();
		const onBringIntoView = vi.fn();
		const closing = closeTabWithSaveGate(doc, { onClose, onBringIntoView });
		expect(onClose).not.toHaveBeenCalled();

		first.reject(new Error('in-flight request failed'));

		await Promise.all([saving, closing]);
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);
		expect(onClose).not.toHaveBeenCalled();
		expect(onBringIntoView).toHaveBeenCalledOnce();
		expect(existingDraft(doc)).toMatchObject({
			editText: 'recoverable local text',
			savedText: 'server text',
			status: 'error',
		});
		clearDraft(doc);
	});

	it('refuses a failed close and retains the exact tab draft for recovery', async () => {
		const doc = target({ path: '/project/failing.md', artifactId: 'failing' });
		mocks.writeContent.mockRejectedValue(new Error('network failed'));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		seedDraft(doc, 'server text');
		setDraftText(doc, 'recoverable local text');

		const onClose = vi.fn();
		const onBringIntoView = vi.fn();
		const result = await closeTabWithSaveGate(doc, { onClose, onBringIntoView });

		expect(result).toEqual({ closed: false });
		expect(onClose).not.toHaveBeenCalled();
		expect(onBringIntoView).toHaveBeenCalledOnce();
		expect(existingDraft(doc)).toMatchObject({
			editText: 'recoverable local text',
			savedText: 'server text',
			status: 'error',
		});
		clearDraft(doc);
	});

	it('keeps the unsaved text and the moved-document message across a retried save', async () => {
		const moved = "Couldn't save: this document is at projects/alpha/shared/planning/plan.md. Your changes are still in this tab. Copy them into the document there.";
		const doc = target({ path: '/project/old-path.md', artifactId: 'moved' });
		mocks.writeContent.mockRejectedValue(new Error(`409 ${JSON.stringify({ detail: moved })}`));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		seedDraft(doc, 'server text');

		setDraftText(doc, 'first edit');
		expect(await flushDraft(doc)).toBe(false);
		setDraftText(doc, 'first edit, then more');
		expect(await flushDraft(doc)).toBe(false);

		expect(mocks.writeContent.mock.calls.map((call) => call[1])).toEqual(['first edit', 'first edit, then more']);
		expect(existingDraft(doc)).toMatchObject({
			editText: 'first edit, then more',
			savedText: 'server text',
			status: 'error',
			error: moved,
		});
		expect(mocks.toastShow).toHaveBeenLastCalledWith(moved, 'error');
		clearDraft(doc);
	});

	it('sends the digest of the text the tab started from, not of the text it writes', async () => {
		const doc = target({ path: '/project/base.md' });
		mocks.writeContent.mockResolvedValue({ saved: true });
		seedDraft(doc, 'Über 漢字 🙂\r\n');
		setDraftText(doc, 'typed');
		await flushDraft(doc);
		expect(expectedOf(mocks.writeContent.mock.calls[0])).toBe(sha('Über 漢字 🙂\r\n'));
		clearDraft(doc);
	});

	it('builds each queued edit on the text the one before it wrote', async () => {
		const doc = target({ path: '/project/queued.md' });
		const first = deferred();
		mocks.writeContent.mockImplementationOnce(() => first.promise).mockResolvedValue({ saved: true });
		seedDraft(doc, 'base');
		setDraftText(doc, 'older');
		const saving = flushDraft(doc);
		await vi.waitFor(() => expect(mocks.writeContent).toHaveBeenCalledTimes(1));
		setDraftText(doc, 'newer');
		first.resolve({ saved: true });
		await saving;
		expect(mocks.writeContent.mock.calls.map(expectedOf)).toEqual([sha('base'), sha('older')]);
		expect(existingDraft(doc)).toMatchObject({ savedText: 'newer', status: 'saved' });
		clearDraft(doc);
	});

	it('hashes the base once while typing goes on, not once per save', async () => {
		const doc = target({ path: '/project/hash-once.md' });
		mocks.writeContent.mockRejectedValue(new Error('network failed'));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const digest = vi.spyOn(crypto.subtle, 'digest');
		seedDraft(doc, 'base');
		for (const text of ['one', 'two', 'three']) {
			setDraftText(doc, text);
			await flushDraft(doc);
		}
		expect(mocks.writeContent).toHaveBeenCalledTimes(3);
		expect(digest).toHaveBeenCalledTimes(1);
		clearDraft(doc);
	});

	it('still saves, with the digest by software, when the browser has no WebCrypto', async () => {
		const doc = target({ path: '/project/no-subtle.md' });
		mocks.writeContent.mockResolvedValue({ saved: true });
		vi.stubGlobal('crypto', {});
		try {
			seedDraft(doc, 'Über 🙂');
			setDraftText(doc, 'typed');
			expect(await flushDraft(doc)).toBe(true);
			expect(expectedOf(mocks.writeContent.mock.calls[0])).toBe(sha('Über 🙂'));
		} finally {
			vi.unstubAllGlobals();
			clearDraft(doc);
		}
	});

	it('saves without a digest when hashing fails, instead of holding the save', async () => {
		const doc = target({ path: '/project/hash-fails.md' });
		mocks.writeContent.mockResolvedValue({ saved: true });
		vi.spyOn(console, 'error').mockImplementation(() => {});
		vi.spyOn(crypto.subtle, 'digest').mockRejectedValue(new Error('no digest'));
		seedDraft(doc, 'base');
		setDraftText(doc, 'typed');
		expect(await flushDraft(doc)).toBe(true);
		expect(expectedOf(mocks.writeContent.mock.calls[0])).toBeNull();
		expect(existingDraft(doc)).toMatchObject({ savedText: 'typed', status: 'saved' });
		clearDraft(doc);
	});

	it('sends no digest for a base that may have been decoded lossily from non-UTF-8 bytes', async () => {
		const doc = target({ path: '/project/lossy.md' });
		mocks.writeContent.mockResolvedValue({ saved: true });
		seedDraft(doc, 'caf�');
		setDraftText(doc, 'cafe');
		await flushDraft(doc);
		expect(expectedOf(mocks.writeContent.mock.calls[0])).toBeNull();
		clearDraft(doc);
	});

	it('refuses a stale base once, keeps the draft and its error, and never sends that base again while typing goes on', async () => {
		const doc = target({ path: '/project/stale.md' });
		mocks.writeContent.mockRejectedValue(staleRefusal());
		vi.spyOn(console, 'error').mockImplementation(() => {});
		seedDraft(doc, 'server text');

		setDraftText(doc, 'first edit');
		expect(await flushDraft(doc)).toBe(false);
		for (const more of ['first edit, more', 'first edit, more again']) {
			setDraftText(doc, more);
			expect(await flushDraft(doc)).toBe(false);
		}

		expect(mocks.writeContent).toHaveBeenCalledTimes(1);
		expect(mocks.toastShow).toHaveBeenCalledTimes(1);
		expect(existingDraft(doc)).toMatchObject({
			editText: 'first edit, more again', savedText: 'server text', status: 'error',
			error: 'This file changed after you started editing.',
		});
		clearDraft(doc);
	});

	it('does not resend the refused base when typing arrives during the refused write', async () => {
		const doc = target({ path: '/project/stale-inflight.md' });
		const slow = deferred();
		mocks.writeContent.mockImplementationOnce(() => slow.promise);
		vi.spyOn(console, 'error').mockImplementation(() => {});
		seedDraft(doc, 'server text');
		setDraftText(doc, 'one');
		const saving = flushDraft(doc);
		await vi.waitFor(() => expect(mocks.writeContent).toHaveBeenCalledTimes(1));
		setDraftText(doc, 'one, and typed during the write');
		slow.reject(staleRefusal());
		expect(await saving).toBe(false);
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);
		expect(existingDraft(doc)).toMatchObject({ editText: 'one, and typed during the write', savedText: 'server text', status: 'error' });
		clearDraft(doc);
	});

	it('refuses a failed close after a stale refusal so the typed text stays in the tab', async () => {
		const doc = target({ path: '/project/stale-close.md' });
		mocks.writeContent.mockRejectedValue(staleRefusal());
		vi.spyOn(console, 'error').mockImplementation(() => {});
		seedDraft(doc, 'server text');
		setDraftText(doc, 'typed');
		const onClose = vi.fn();
		const onBringIntoView = vi.fn();
		expect(await closeTabWithSaveGate(doc, { onClose, onBringIntoView })).toEqual({ closed: false });
		expect(await closeTabWithSaveGate(doc, { onClose, onBringIntoView })).toEqual({ closed: false });
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);
		expect(onClose).not.toHaveBeenCalled();
		expect(existingDraft(doc)).toMatchObject({ editText: 'typed', status: 'error' });
		clearDraft(doc);
	});

	it('lets a different base save, and an ordinary failure still retries', async () => {
		const stale = target({ path: '/project/other-base.md' });
		mocks.writeContent.mockRejectedValueOnce(staleRefusal()).mockResolvedValue({ saved: true });
		vi.spyOn(console, 'error').mockImplementation(() => {});
		seedDraft(stale, 'server text');
		setDraftText(stale, 'typed');
		expect(await flushDraft(stale)).toBe(false);
		// The text goes back to the base: the draft is clean, so a reload may seed the file's new text.
		setDraftText(stale, 'server text');
		expect(existingDraft(stale)).toMatchObject({ status: 'idle', error: '' });
		seedDraft(stale, 'what the file holds now');
		setDraftText(stale, 'typed on the new base');
		expect(await flushDraft(stale)).toBe(true);
		expect(mocks.writeContent.mock.calls.map(expectedOf)).toEqual([sha('server text'), sha('what the file holds now')]);
		clearDraft(stale);

		const flaky = target({ path: '/project/flaky.md' });
		mocks.writeContent.mockReset();
		mocks.writeContent.mockRejectedValueOnce(new Error('network failed')).mockResolvedValue({ saved: true });
		seedDraft(flaky, 'base');
		setDraftText(flaky, 'typed');
		expect(await flushDraft(flaky)).toBe(false);
		expect(await flushDraft(flaky)).toBe(true);
		expect(mocks.writeContent).toHaveBeenCalledTimes(2);
		clearDraft(flaky);
	});

	it('frees the saved text a returning tab paints from once the tab closes', async () => {
		const doc = target({ path: '/project/closing.md', artifactId: 'closing' });
		seedDraft(doc, 'saved text');

		const onClose = vi.fn();
		const result = await closeTabWithSaveGate(doc, { onClose, onBringIntoView: vi.fn() });

		expect(result).toEqual({ closed: true });
		expect(onClose).toHaveBeenCalledOnce();
		expect(existingDraft(doc)).toBeUndefined();
	});

	it('clears a failed draft when the user restores the last saved text', async () => {
		const doc = target({ path: '/project/reverted.md', artifactId: 'reverted' });
		mocks.writeContent.mockRejectedValue(new Error('network failed'));
		vi.spyOn(console, 'error').mockImplementation(() => {});
		seedDraft(doc, 'server text');
		setDraftText(doc, 'unsaved text');

		expect(await flushDraft(doc)).toBe(false);
		expect(existingDraft(doc)).toMatchObject({ status: 'error', error: expect.any(String) });

		setDraftText(doc, 'server text');

		expect(existingDraft(doc)).toMatchObject({
			editText: 'server text',
			savedText: 'server text',
			status: 'idle',
			error: '',
		});
		expect(await flushDraft(doc)).toBe(true);
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);
		clearDraft(doc);
	});
});
