import { beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({ writeContent: vi.fn() }));
vi.mock('$lib/api/docs', () => api);
vi.mock('$lib/stores/docComments', () => ({ flushPositions: vi.fn() }));

import { applyTabRevisionCheck, openArtifactTab, threadWorkspaces } from './artifactWorkspace';
import { clearDraft, docSaveDrafts, existingDraft, flushDraft, seedDraft, setDraftText } from './docSaveQueue';

describe('a relocated tab and its unsaved draft', () => {
	beforeEach(() => {
		threadWorkspaces.set({});
		docSaveDrafts.set({});
		api.writeContent.mockReset();
		api.writeContent.mockResolvedValue({ saved: true, revision: 'r2' });
	});

	// The tab's path changes in the store and nothing else has run yet: no mounted leaf.
	function relocatedWithUnsavedText() {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a.md' });
		const tab = get(threadWorkspaces)['thread-1'].tabs[0];
		const doc = { instanceId: tab.instanceId, path: tab.path, name: tab.title, projectId: null, threadId: 'thread-1', artifactId: 'art-1' };
		seedDraft(doc, 'saved');
		setDraftText(doc, 'unsaved');
		applyTabRevisionCheck('thread-1', tab.instanceId, {
			exists: true, path: '/archive/b.md', revision: 'r2', writable: true, artifactId: 'art-1',
		});
		return doc;
	}

	it('saves unsaved text to the path the tab now holds, never the old one', async () => {
		const doc = relocatedWithUnsavedText();

		await flushDraft(doc);

		expect(api.writeContent).toHaveBeenCalledOnce();
		expect(api.writeContent.mock.calls[0][0]).toBe('/archive/b.md');
		clearDraft(doc);
	});

	it('keeps the unsaved text and its tab-instance key while the path moves', () => {
		const doc = relocatedWithUnsavedText();

		expect(existingDraft(doc)).toMatchObject({ editText: 'unsaved', target: { path: '/archive/b.md', artifactId: 'art-1' } });
		clearDraft(doc);
	});
});
