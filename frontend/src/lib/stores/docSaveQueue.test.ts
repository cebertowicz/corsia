import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const mocks = vi.hoisted(() => ({
	writeContent: vi.fn(),
	flushPositions: vi.fn(),
	toastShow: vi.fn(),
}));

vi.mock('$lib/api/docs', () => ({ writeContent: mocks.writeContent }));
vi.mock('$lib/stores/docComments', () => ({ flushPositions: mocks.flushPositions }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.toastShow } }));

import {
	clearDraft, closeTabWithSaveGate, docSaveDrafts, draftKey, effectiveDraftTarget,
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
		expect(effectiveDraftTarget(pathOnly)).toMatchObject({
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
