import { afterEach, describe, expect, it, vi } from 'vitest';
import { createHash } from 'node:crypto';

const sha = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');
const expectedOf = (call: unknown[]) => (call[2] as { expectedSha256?: string | null }).expectedSha256;
const textOf = (call: unknown[]) => call[1] as string;
const staleRefusal = () => new Error(`409 ${JSON.stringify({ detail: { code: 'stale_base', message: 'Plain sentence.' } })}`);

const mocks = vi.hoisted(() => ({ writeContent: vi.fn(), flushPositions: vi.fn(), toastShow: vi.fn() }));
vi.mock('$lib/api/docs', () => ({ writeContent: mocks.writeContent }));
vi.mock('$lib/stores/docComments', () => ({ flushPositions: mocks.flushPositions }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.toastShow } }));

import { clearDraft, draftKey, existingDraft, flushDraft, keepMyText, seedDraft, setDraftText, useFileText } from './docSaveQueue';
import { EDITOR_STALE_STATUS, staleToast } from './docSaveBase';
import { docSaveBars } from './docSaveBars';
import type { DocViewerTarget } from '$lib/docViewerTarget';

let seq = 0;
const target = (): DocViewerTarget => ({ instanceId: `stale-tab-${++seq}`, path: '/project/note.md', name: 'note.md', projectId: 'p', threadId: 't', artifactId: 'a' });

/** A stale refusal on `base`, leaving the tab's text `mine` unsaved. */
async function refused(base: string, mine: string) {
	const doc = target();
	mocks.writeContent.mockRejectedValueOnce(staleRefusal());
	seedDraft(doc, base);
	setDraftText(doc, mine);
	expect(await flushDraft(doc)).toBe(false);
	return doc;
}

afterEach(() => {
	vi.restoreAllMocks();
	mocks.writeContent.mockReset();
	mocks.toastShow.mockReset();
	docSaveBars.reset();
});

describe('the choice after a stale refusal', () => {
	it('words the tab’s bar itself and marks the draft as the one to choose for', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await refused('base', 'mine');
		expect(existingDraft(doc)).toMatchObject({ status: 'error', stale: true, error: EDITOR_STALE_STATUS });
		clearDraft(doc);
	});

	it('keeps the bar through typing instead of clearing it with each key', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await refused('base', 'mine');
		setDraftText(doc, 'mine, and more');
		expect(existingDraft(doc)).toMatchObject({ status: 'error', stale: true, error: EDITOR_STALE_STATUS });
		expect(await flushDraft(doc)).toBe(false);
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);
		clearDraft(doc);
	});

	it('Keep my text saves the tab’s text against the digest of the file text just read', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await refused('base', 'mine');
		mocks.writeContent.mockResolvedValueOnce({ saved: true, revision: 'r2' });

		expect(await keepMyText(doc, 'what the file holds now')).toBe(true);

		const call = mocks.writeContent.mock.calls[1];
		expect(textOf(call)).toBe('mine');
		expect(expectedOf(call)).toBe(sha('what the file holds now'));
		expect(existingDraft(doc)).toMatchObject({ editText: 'mine', savedText: 'mine', status: 'saved', error: '', stale: false, savedRevision: 'r2' });
		clearDraft(doc);
	});

	it('refuses again when the file changed again: his text stays, the new base is refused once, and the bar returns', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await refused('base', 'mine');
		mocks.writeContent.mockRejectedValueOnce(staleRefusal());

		expect(await keepMyText(doc, 'file, second version')).toBe(false);

		expect(existingDraft(doc)).toMatchObject({ editText: 'mine', savedText: 'file, second version', status: 'error', stale: true, error: EDITOR_STALE_STATUS });
		expect(expectedOf(mocks.writeContent.mock.calls[1])).toBe(sha('file, second version'));
		setDraftText(doc, 'mine, typed on');
		expect(await flushDraft(doc)).toBe(false);
		expect(mocks.writeContent).toHaveBeenCalledTimes(2);
		clearDraft(doc);
	});

	it('keeps his text and says why when the save fails for another reason', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await refused('base', 'mine');
		mocks.writeContent.mockRejectedValueOnce(new Error('network failed'));
		expect(await keepMyText(doc, 'file now')).toBe(false);
		expect(existingDraft(doc)).toMatchObject({ editText: 'mine', status: 'error', stale: false });
		expect(existingDraft(doc)?.error).not.toBe('');
		clearDraft(doc);
	});

	it('lets a save through when the file went back to the refused text, only because the choice deletes the refusal', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await refused('B', 'mine');
		// Without the choice the refused base still blocks: the file reads 'B' again, and so does the tab's base.
		expect(await flushDraft(doc)).toBe(false);
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);

		mocks.writeContent.mockResolvedValueOnce({ saved: true });
		expect(await keepMyText(doc, 'B')).toBe(true);
		expect(mocks.writeContent).toHaveBeenCalledTimes(2);
		expect(expectedOf(mocks.writeContent.mock.calls[1])).toBe(sha('B'));
		clearDraft(doc);
	});

	it('Use the file’s text makes the file’s text the tab’s text and base, and the refusal ends', async () => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
		const doc = await refused('B', 'mine');
		useFileText(doc, { text: 'B', revision: 'r9' });

		expect(existingDraft(doc)).toMatchObject({ editText: 'B', savedText: 'B', status: 'idle', error: '', stale: false, savedRevision: 'r9' });
		expect(await flushDraft(doc)).toBe(true);
		expect(mocks.writeContent).toHaveBeenCalledTimes(1);
		// The file reads as the refused text again, and typing on it saves: the refusal is gone, not just hidden.
		mocks.writeContent.mockResolvedValueOnce({ saved: true });
		setDraftText(doc, 'B, then more');
		expect(await flushDraft(doc)).toBe(true);
		expect(expectedOf(mocks.writeContent.mock.calls[1])).toBe(sha('B'));
		clearDraft(doc);
	});

	it('Use the file’s text cancels a save that was waiting on its pause', async () => {
		vi.useFakeTimers();
		try {
			vi.spyOn(console, 'error').mockImplementation(() => {});
			const doc = await refused('base', 'mine');
			setDraftText(doc, 'mine, typed just now');
			useFileText(doc, { text: 'file', revision: null });
			await vi.advanceTimersByTimeAsync(2000);
			expect(mocks.writeContent).toHaveBeenCalledTimes(1);
			clearDraft(doc);
		} finally { vi.useRealTimers(); }
	});
});

describe('where a save error is said', () => {
	function bar(rect: Partial<DOMRect>): Element {
		const element = document.createElement('div');
		element.getBoundingClientRect = () => ({ left: 0, top: 0, right: 400, bottom: 300, width: 400, height: 300, x: 0, y: 0, toJSON: () => ({}), ...rect }) as DOMRect;
		return element;
	}
	async function failOnce(doc: DocViewerTarget, error: Error) {
		mocks.writeContent.mockRejectedValueOnce(error);
		seedDraft(doc, 'base');
		setDraftText(doc, 'typed');
		vi.spyOn(console, 'error').mockImplementation(() => {});
		expect(await flushDraft(doc)).toBe(false);
	}

	it('says it in the bar alone when that document’s bar is on screen, for a stale refusal and any other failure', async () => {
		for (const error of [staleRefusal(), new Error('network failed')]) {
			const doc = target();
			const drop = docSaveBars.shown(draftKey(doc), bar({}));
			await failOnce(doc, error);
			expect(mocks.toastShow).not.toHaveBeenCalled();
			drop();
			clearDraft(doc);
		}
	});

	it('toasts when the document is closed, so no bar is registered', async () => {
		const doc = target();
		await failOnce(doc, new Error('network failed'));
		expect(mocks.toastShow).toHaveBeenCalledTimes(1);
		clearDraft(doc);
	});

	it('toasts when the tab is open but hidden behind another tab: its bar takes no room', async () => {
		const doc = target();
		docSaveBars.shown(draftKey(doc), bar({ width: 0, height: 0, right: 0, bottom: 0 }));
		await failOnce(doc, staleRefusal());
		expect(mocks.toastShow).toHaveBeenCalledWith('Couldn’t save note.md because it changed after you started editing. Open it to choose which text to keep.', 'error');
		expect(staleToast('')).toBe('Couldn’t save a document because it changed after you started editing. Open it to choose which text to keep.');
		clearDraft(doc);
	});

	it('toasts for a tab whose bar is on screen only for another document', async () => {
		const doc = target();
		docSaveBars.shown(draftKey(target()), bar({}));
		await failOnce(doc, new Error('network failed'));
		expect(mocks.toastShow).toHaveBeenCalledTimes(1);
		clearDraft(doc);
	});
});
