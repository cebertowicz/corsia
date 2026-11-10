import { afterEach, describe, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';
import { DocOpenError } from '$lib/docOpenRefusal';
import { clearDraft, seedDraft, setDraftText } from '$lib/stores/docSaveQueue';
import { commentDraft, commentDraftScope } from '$lib/stores/commentDraft';
import { liveDocumentBindings, openLiveDocument } from '$lib/stores/liveDocuments';
import { leavePreviousTarget, loadViewerContent, openFromDraft, viewerContent } from './docViewerLoading';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), writeContent: vi.fn() }));
vi.mock('$lib/api/docs', () => api);
vi.mock('$lib/stores/liveDocuments', () => ({
	openLiveDocument: vi.fn(), closeLiveDocument: vi.fn(), liveDocumentBindings: writable({}),
}));
vi.mock('$lib/stores/docComments', () => ({ flushPositions: vi.fn() }));

const target = { instanceId: 'loading', path: '/documents/doc.md', name: 'doc.md', projectId: null, threadId: 'thread', artifactId: 'artifact' };
const setupFailed = { code: 'setup_failed' as const, title: 'Setup failed', message: 'Document setup failed; open it again to retry' };

afterEach(() => { clearDraft(target); vi.clearAllMocks(); });

async function refusedBeforeAnySnapshot() {
	api.fetchContent.mockRejectedValue(new DocOpenError(setupFailed));
	const onSnapshot = vi.fn();
	const error = await loadViewerContent({ target, isCurrent: () => true, onReady: vi.fn(), onSnapshot, onUnavailable: vi.fn() })
		.then(() => null, (thrown: unknown) => thrown);
	return { error, onSnapshot };
}

describe('a live open refused before the file snapshot arrives', () => {
	it('with no draft clears the editor instead of leaving the previous document on screen, and keeps the server body', async () => {
		const { error, onSnapshot } = await refusedBeforeAnySnapshot();
		expect(onSnapshot).toHaveBeenCalledExactlyOnceWith('');
		expect(error).toBeInstanceOf(DocOpenError);
		expect(error).toMatchObject({ code: 'setup_failed', title: setupFailed.title, message: "Live editing couldn't start for this document." });
	});

	it('with a draft leaves the draft to the viewer and keeps the server body', async () => {
		seedDraft(target, 'tab draft');
		const { error, onSnapshot } = await refusedBeforeAnySnapshot();
		expect(onSnapshot).not.toHaveBeenCalled();
		expect(error).toMatchObject({ code: 'setup_failed', message: "Live editing couldn't start for this document." });
	});
});

describe('a document opened over HTTP', () => {
	it('offers History only when the server says the file has recorded versions', () => {
		const response = { text: 't', size: 1, mtime: 0, revision: 'r', writable: true, artifact: null, freshness: null };
		expect(viewerContent({ ...response, history: true }, null).history).toBe(true);
		expect(viewerContent(response, null).history).toBe(false);
	});
});

describe('a tab whose document relocated before its draft followed', () => {
	it('loads the tab path, taking only the artifact identity from the draft', async () => {
		const relocated = { ...target, path: '/archive/doc.md' };
		seedDraft(target, '# Saved');
		api.fetchContent.mockImplementation(async (path: string) => {
			if (path === target.path) throw new Error('404 not found');
			return { text: '# Relocated', writable: true, freshness: null, revision: 'r2', artifact: { id: 'artifact' } };
		});

		const onReady = vi.fn();
		await loadViewerContent({ target: relocated, isCurrent: () => true, onReady, onSnapshot: vi.fn(), onUnavailable: vi.fn() });

		expect(api.fetchContent).toHaveBeenCalledExactlyOnceWith(relocated.path, expect.anything());
		expect(onReady).toHaveBeenCalledWith(expect.objectContaining({ text: '# Relocated', artifactId: 'artifact' }));
	});
});

describe('a live reopen that replayed a device copy', () => {
	const fileText = '# Doc\nfirst';
	const openWith = async (liveText: string, unwritten: boolean, history?: boolean, onReady = vi.fn()) => {
		const flush = vi.fn().mockResolvedValue({});
		const written = vi.fn();
		const live = { text: { toString: () => liveText }, artifactId: 'artifact', writable: true, flush,
			durability: { clean: true, unwritten, written, snapshot: () => ({ localGeneration: 3, ackGeneration: 1 }) }, connection: 'connected' };
		api.fetchContent.mockResolvedValue({ text: fileText, writable: true, freshness: null, revision: 'r1', artifact: { id: 'artifact' }, history });
		vi.mocked(openLiveDocument).mockResolvedValue(live as never);
		liveDocumentBindings.set({ [target.instanceId]: live } as never);
		seedDraft(target, liveText);
		await loadViewerContent({ target, isCurrent: () => true, onReady, onSnapshot: vi.fn(), onUnavailable: vi.fn() });
		await new Promise(resolve => setTimeout(resolve, 0));
		return Object.assign(flush, { written });
	};
	afterEach(() => { liveDocumentBindings.set({}); });

	it('asks for the file write when the replay left the file behind the live text', async () => {
		const flush = await openWith('# Doc\nfirst\nsecond', true);
		expect(flush).toHaveBeenCalledOnce();
	});

	it('writes nothing when the replay added nothing to the file text, and owes no flush on close', async () => {
		const flush = await openWith(fileText, true);
		expect(flush).not.toHaveBeenCalled();
		expect(flush.written).toHaveBeenCalledExactlyOnceWith(3);
	});

	it('offers History on a live open only when the server says the file is recorded', async () => {
		const recorded = vi.fn();
		await openWith(fileText, false, true, recorded);
		expect(recorded).toHaveBeenCalledWith(expect.objectContaining({ history: true }));
		const unrecorded = vi.fn();
		await openWith(fileText, false, undefined, unrecorded);
		expect(unrecorded).toHaveBeenCalledWith(expect.objectContaining({ history: false }));
	});

	it('writes nothing on a plain reopen with no local transactions to write', async () => {
		const flush = await openWith('# Doc\nfirst\nremote edit', false);
		expect(flush).not.toHaveBeenCalled();
		expect(flush.written).not.toHaveBeenCalled();
	});
});

describe('switching the viewer to another tab', () => {
	const next = { ...target, instanceId: 'next', path: '/documents/next.md' };
	afterEach(() => { clearDraft(next); commentDraft.set(null); });

	it('saves the previous tab’s typing and drops its comment draft, and returns the tab now shown', async () => {
		api.writeContent.mockResolvedValue({ ok: true, sha256: 'saved', revision: 'saved' });
		seedDraft(target, '# Saved', 'r1');
		setDraftText(target, '# Typed');
		commentDraft.set({ from: 0, to: 1, text: '#', top: 0, scope: commentDraftScope(target) });
		expect(leavePreviousTarget(target, next)).toBe(next);
		expect(get(commentDraft)).toBeNull();
		await vi.waitFor(() => expect(api.writeContent).toHaveBeenCalledTimes(1));
		expect(api.writeContent.mock.calls[0]).toContain('# Typed');
	});

	it('touches nothing when the same tab is shown again or none was shown', () => {
		const draft = { from: 0, to: 1, text: '#', top: 0, scope: commentDraftScope(target) };
		commentDraft.set(draft);
		expect(leavePreviousTarget(target, target)).toBe(target);
		expect(leavePreviousTarget(null, next)).toBe(next);
		expect(get(commentDraft)).toBe(draft);
		expect(api.writeContent).not.toHaveBeenCalled();
	});
});

describe('a tab opening from its draft', () => {
	const draft = (editText: string, savedText: string) => ({ target, editText, savedText, status: 'idle' as const }) as unknown as Parameters<typeof openFromDraft>[0];

	it('waits for the loader when its draft holds nothing', () => {
		expect(openFromDraft(undefined, 'artifact')).toEqual({ text: null, unsaved: false, confirming: false, signal: { status: 'loading' } });
	});

	it('shows saved text at once while the loader confirms it, and claims nothing about write access meanwhile', () => {
		const opened = openFromDraft(draft('# Saved', '# Saved'), 'artifact');
		expect(opened).toMatchObject({ text: '# Saved', unsaved: false, confirming: true });
		expect(opened.signal).toEqual({ status: 'ready', artifactId: 'artifact', shown: true });
	});

	it('shows unsaved text at once, read-only until the loader re-checks write access', () => {
		const opened = openFromDraft(draft('# Local', '# Saved'), 'artifact');
		expect(opened).toMatchObject({ text: '# Local', unsaved: true, confirming: false });
		expect(opened.signal).toEqual({ status: 'ready', artifactId: 'artifact', shown: true, writable: false });
	});
});
