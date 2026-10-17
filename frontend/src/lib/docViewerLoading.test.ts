import { afterEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import { DocOpenError } from '$lib/docOpenRefusal';
import { clearDraft, seedDraft } from '$lib/stores/docSaveQueue';
import { loadViewerContent, openFromDraft } from './docViewerLoading';

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
