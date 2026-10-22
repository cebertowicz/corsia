import { beforeEach, describe, expect, it, vi } from 'vitest';

import { acceptAllOpen, openSuggestionCount } from './docAcceptAll';
import { SuggestionRefused } from '$lib/api/commentSuggestions';
import type { DocComment } from '$lib/stores/docComments';
import type { DocViewerTarget } from '$lib/docViewerTarget';

const mocks = vi.hoisted(() => ({ acceptAllSuggestions: vi.fn(), flushDraft: vi.fn(), seedDraft: vi.fn(), loadComments: vi.fn() }));
vi.mock('$lib/api/commentSuggestions', async (importOriginal) => ({ ...(await importOriginal<object>()), acceptAllSuggestions: mocks.acceptAllSuggestions }));
vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: mocks.flushDraft, seedDraft: mocks.seedDraft }));
vi.mock('$lib/stores/docComments', () => ({ loadComments: mocks.loadComments }));

const target = { instanceId: 'tab-1' } as DocViewerTarget;
const comment = (patch: Partial<DocComment>) => ({ resolved: false, orphaned: false, replacement: 'x', ...patch }) as DocComment;
const hooks = () => ({ onText: vi.fn(), onDocumentChanged: vi.fn() });

beforeEach(() => {
	Object.values(mocks).forEach((mock) => mock.mockReset());
	mocks.flushDraft.mockResolvedValue(true);
	mocks.loadComments.mockResolvedValue({});
});

describe('openSuggestionCount', () => {
	it('counts open suggestions, not comments, decided ones or orphaned ones', () => {
		expect(openSuggestionCount([
			comment({}), comment({ replacement: '' }), comment({ replacement: null }), comment({ resolved: true }), comment({ orphaned: true }),
		])).toBe(2);
	});
});

describe('acceptAllOpen', () => {
	it('saves the document, accepts, puts the accepted text on screen and reloads the comments', async () => {
		mocks.acceptAllSuggestions.mockResolvedValue({ accepted: ['c1'], skipped: [], text: 'new text', revision: 'r2' });
		const h = hooks();
		await acceptAllOpen(target, 'art-1', h);
		expect(mocks.flushDraft).toHaveBeenCalledWith(target);
		expect(mocks.acceptAllSuggestions).toHaveBeenCalledWith('art-1');
		expect(mocks.seedDraft).toHaveBeenCalledWith(target, 'new text', 'r2');
		expect(h.onText).toHaveBeenCalledWith('new text');
		expect(mocks.loadComments).toHaveBeenCalledWith('art-1');
	});

	it('changes nothing on screen when there was nothing to accept', async () => {
		mocks.acceptAllSuggestions.mockResolvedValue({ accepted: [], skipped: [], text: null, revision: null });
		const h = hooks();
		await acceptAllOpen(target, 'art-1', h);
		expect(h.onText).not.toHaveBeenCalled();
	});

	it('refuses to accept over text it could not save', async () => {
		mocks.flushDraft.mockResolvedValue(false);
		await expect(acceptAllOpen(target, 'art-1', hooks())).rejects.toThrow(/save the document/);
		expect(mocks.acceptAllSuggestions).not.toHaveBeenCalled();
	});

	it('reads the document again when the edit was applied but not recorded, and still fails', async () => {
		mocks.acceptAllSuggestions.mockRejectedValue(new SuggestionRefused('unrecorded', 409, 'unrecorded'));
		const h = hooks();
		await expect(acceptAllOpen(target, 'art-1', h)).rejects.toBeInstanceOf(SuggestionRefused);
		expect(h.onDocumentChanged).toHaveBeenCalled();
		expect(h.onText).not.toHaveBeenCalled();
	});
});
